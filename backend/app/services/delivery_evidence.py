"""Persist actual canonical writes in the same normalization transaction."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import (
    FuturesContract,
    FuturesContractEOD,
    Instrument,
    MarketDataEOD,
    MarketDataMinute,
)
from app.models.registry import DailyDeliveryMember, DeliveryMemberEvidence, IngestionRun
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
    else:
        return
    expected = (
        set(
            await db.scalars(
                select(DailyDeliveryMember.member_key).where(
                    DailyDeliveryMember.part_id == run.delivery_part_id
                )
            )
        )
        if run.delivery_part_id
        else written
    )
    values = [
        {"run_id": run.run_id, "member_key": key, "created_at": utc_now()}
        for key in sorted(written & expected)
    ]
    if values:
        await db.execute(insert(DeliveryMemberEvidence).values(values).on_conflict_do_nothing())
