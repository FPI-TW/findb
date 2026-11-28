"""Persist actual canonical writes in the same normalization transaction."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import (
    ExchangeDailyQuote,
    FuturesContract,
    FuturesContractEOD,
    Instrument,
    IntradayPartition,
    MarketDataEOD,
    MarketDataMinute,
)
from app.models.institutional_flow import InstitutionalFlowPublication
from app.models.raw import RawMarketPayload
from app.models.registry import DailyDeliveryMember, DeliveryMemberEvidence, IngestionRun
from app.schemas.ingress import (
    INDEX_REFERENCE_SCHEMAS,
    INSTITUTIONAL_FLOW_SCHEMAS,
    index_reference_member,
)
from app.utils import utc_now


def run_row_dates(run: IngestionRun) -> tuple[date, date] | None:
    """The trade dates a contract run's rows can hold, or None when unknown.

    Ingress keeps every EOD, futures and exchange-quote row on ``data_date`` or
    inside the declared coverage. The canonical tables index ``trade_date`` but
    not ``run_id``, and ``market_data_eod`` is partitioned by year: without this
    range a run lookup scans every partition, a cost that grows with history.
    A legacy run without batch dates keeps the unbounded lookup.
    """
    dates = [
        value
        for value in (run.batch_data_date, run.coverage_start_date, run.coverage_end_date)
        if value is not None
    ]
    return (min(dates), max(dates)) if dates else None


async def record_delivery_evidence(db: AsyncSession, run: IngestionRun) -> None:
    """Counts alone never establish delivery; record only rows actually upserted."""
    if run.status not in {"completed", "completed_with_errors"}:
        return
    dates = run_row_dates(run)

    def on_run_dates(model):
        return () if dates is None else (model.trade_date.between(*dates),)

    if run.schema_id == "futures_eod":
        rows = await db.execute(
            select(Instrument.symbol, FuturesContract.contract_code, FuturesContractEOD.session)
            .join(FuturesContract, FuturesContract.instrument_id == Instrument.instrument_id)
            .join(FuturesContractEOD, FuturesContractEOD.contract_id == FuturesContract.contract_id)
            .where(FuturesContractEOD.run_id == run.run_id, *on_run_dates(FuturesContractEOD))
        )
        written = {f"{symbol}|{contract}|{session}" for symbol, contract, session in rows}
    elif run.schema_id == "market_eod":
        written = set(
            await db.scalars(
                select(Instrument.symbol)
                .join(MarketDataEOD, MarketDataEOD.instrument_id == Instrument.instrument_id)
                .where(MarketDataEOD.run_id == run.run_id, *on_run_dates(MarketDataEOD))
                .distinct()
            )
        )
    elif run.schema_id == "market_minute":
        # Retired Shioaji minute bars: ingress does not tie their dates to the batch.
        written = set(
            await db.scalars(
                select(Instrument.symbol)
                .join(MarketDataMinute, MarketDataMinute.instrument_id == Instrument.instrument_id)
                .where(MarketDataMinute.run_id == run.run_id)
                .distinct()
            )
        )
    elif run.schema_id == "intraday_partition":
        # The index row names the run whose delivery is the stored day.
        written = set(
            await db.scalars(
                select(IntradayPartition.symbol).where(IntradayPartition.run_id == run.run_id)
            )
        )
    elif run.schema_id in INSTITUTIONAL_FLOW_SCHEMAS:
        # A stored table's version names its run; the board is the plan member.
        written = set(
            await db.scalars(
                select(InstitutionalFlowPublication.board).where(
                    InstitutionalFlowPublication.run_id == run.run_id
                )
            )
        )
    elif run.schema_id in INDEX_REFERENCE_SCHEMAS:
        # A document part stands for its member once it is kept: stored by this run,
        # or already stored by an earlier delivery of it (repeats store nothing).
        # A rejected document completes with errors and stands for none.
        raw = (
            await db.get(RawMarketPayload, run.raw_payload_id)
            if run.status == "completed" and run.raw_payload_id is not None
            else None
        )
        written = {index_reference_member(str(run.schema_id), raw.payload)} if raw else set()
    elif run.schema_id == "exchange_daily_quote":
        written = set(
            await db.scalars(
                select(Instrument.symbol)
                .join(
                    ExchangeDailyQuote, ExchangeDailyQuote.instrument_id == Instrument.instrument_id
                )
                .where(ExchangeDailyQuote.run_id == run.run_id, *on_run_dates(ExchangeDailyQuote))
            )
        )
    else:
        return
    from app.services.provider_metadata import delivers_unlisted

    # Admission already bound every row to this part, to the plan's frozen
    # supplement or to no plan member at all; unlisted writes are recorded too.
    expected = written
    if run.delivery_part_id and not delivers_unlisted(run.source or "", run.dataset_key):
        from app.models.registry import DailyDeliveryPart, DailyDeliveryPlan
        from app.services.universe_supplement import plan_supplement

        part = await db.get(DailyDeliveryPart, run.delivery_part_id)
        plan = await db.get(DailyDeliveryPlan, part.plan_id) if part else None
        expected = set(
            await db.scalars(
                select(DailyDeliveryMember.member_key).where(
                    DailyDeliveryMember.part_id == run.delivery_part_id
                )
            )
        ) | (set(await plan_supplement(db, plan)) if plan else set())
    values = [
        {"run_id": run.run_id, "member_key": key, "created_at": utc_now()}
        for key in sorted(written & expected)
    ]
    if values:
        await db.execute(insert(DeliveryMemberEvidence).values(values).on_conflict_do_nothing())
