"""Persist actual canonical writes in the same normalization transaction."""

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
from app.models.registry import DailyDeliveryMember, DeliveryMemberEvidence, IngestionRun
from app.schemas.ingress import INSTITUTIONAL_FLOW_SCHEMAS
from app.utils import utc_now


async def record_delivery_evidence(db: AsyncSession, run: IngestionRun) -> None:
    """Counts alone never establish delivery; record only rows actually upserted."""
    if run.status not in {"completed", "completed_with_errors"}:
        return
    if run.schema_id == "futures_eod":
        rows = await db.execute(
            select(Instrument.symbol, FuturesContract.contract_code, FuturesContractEOD.session)
            .join(FuturesContract, FuturesContract.instrument_id == Instrument.instrument_id)
            .join(FuturesContractEOD, FuturesContractEOD.contract_id == FuturesContract.contract_id)
            .where(FuturesContractEOD.run_id == run.run_id)
        )
        written = {f"{symbol}|{contract}|{session}" for symbol, contract, session in rows}
    elif run.schema_id in {"market_eod", "market_minute"}:
        model = MarketDataEOD if run.schema_id == "market_eod" else MarketDataMinute
        written = set(
            await db.scalars(
                select(Instrument.symbol)
                .join(model, model.instrument_id == Instrument.instrument_id)
                .where(model.run_id == run.run_id)
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
    elif run.schema_id == "exchange_daily_quote":
        written = set(
            await db.scalars(
                select(Instrument.symbol)
                .join(
                    ExchangeDailyQuote, ExchangeDailyQuote.instrument_id == Instrument.instrument_id
                )
                .where(ExchangeDailyQuote.run_id == run.run_id)
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
