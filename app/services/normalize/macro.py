"""
Macro data normalizer.
"""

from datetime import date
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.canonical import MacroObservation, MacroSeries
from app.services.dq.validators import DQIssueRecord
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.types import MacroObservationRecord
from app.utils import utc_now, uuid7


def _merge_config(default: dict, override: dict | None) -> dict:
    """Merge dataset config with defaults."""
    if not override:
        return {**default}

    merged = {**default, **override}
    if "field_mapping" in override:
        merged["field_mapping"] = override["field_mapping"]
    return merged


def _candidate_paths(primary: str | None, fallbacks: list[str]) -> list[str]:
    """Build candidate paths with fallbacks."""
    paths: list[str] = []
    if primary:
        paths.append(primary)
    for fallback in fallbacks:
        if fallback not in paths:
            paths.append(fallback)
    return paths


class MacroNormalizer(BaseNormalizer):
    """
    Normalizer for macro series observations.
    """

    dataset_key = "macro_observation"
    asset_class = "macro"
    market = "MACRO"

    DEFAULT_CONFIG = {
        "data_path": "data",
        "field_mapping": {
            "source_code": "source_code",
            "name": "name",
            "unit": "unit",
            "frequency": "frequency",
            "market": "market",
            "obs_date": "date",
            "value": "value",
            "source": "source",
        },
    }

    def _resolve_value(self, item: dict, raw_data: dict, paths: list[str]) -> Any:
        """Resolve a value from item or payload using candidate paths."""
        for path in paths:
            value = self._get_nested_value(item, path)
            if value is not None:
                return value
        for path in paths:
            value = self._get_nested_value(raw_data, path)
            if value is not None:
                return value
        for path in paths:
            value = self._get_nested_value(raw_data, f"metadata.{path}")
            if value is not None:
                return value
        return None

    def _parse_obs_date(self, value: Any) -> Optional[date]:
        """Parse observation date into date object."""
        dt = self._parse_trade_date(value)
        return dt.date() if dt else None

    def _normalize_record(self, record: MacroObservationRecord) -> MacroObservationRecord:
        """Normalize series fields."""
        if record.source_code:
            record.source_code = str(record.source_code).strip()
        if record.name:
            record.name = str(record.name).strip()
        if record.unit:
            record.unit = str(record.unit).strip()
        if record.frequency:
            record.frequency = str(record.frequency).lower().strip()
        if record.market:
            record.market = str(record.market).upper().strip()
        if record.source:
            record.source = str(record.source).lower().strip()
        return record

    def map_fields(self, raw_data: dict) -> list[MacroObservationRecord]:
        """Map macro payload to canonical format."""
        config = _merge_config(self.DEFAULT_CONFIG, getattr(self, "dataset_config", None))
        field_mapping = config.get("field_mapping", {}) or {}
        data_path = config.get("data_path", "data")

        data_items = self._get_nested_value(raw_data, data_path) if data_path else None
        if data_items is None:
            data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        records: list[MacroObservationRecord] = []
        for item in data_items:
            source_code = self._resolve_value(
                item,
                raw_data,
                _candidate_paths(
                    field_mapping.get("source_code"),
                    [
                        "source_code",
                        "series_code",
                        "series.code",
                        "series_id",
                        "series.id",
                        "ticker",
                        "symbol",
                        "index_id",
                        "bond_id",
                        "rate_id",
                        "stock_id",
                        "code",
                    ],
                ),
            )
            name = self._resolve_value(
                item,
                raw_data,
                _candidate_paths(
                    field_mapping.get("name"),
                    ["name", "series.name", "series_name"],
                ),
            )
            unit = self._resolve_value(
                item,
                raw_data,
                _candidate_paths(field_mapping.get("unit"), ["unit", "series.unit"]),
            )
            frequency = self._resolve_value(
                item,
                raw_data,
                _candidate_paths(
                    field_mapping.get("frequency"),
                    ["frequency", "freq", "series.frequency"],
                ),
            )
            market = self._resolve_value(
                item,
                raw_data,
                _candidate_paths(field_mapping.get("market"), ["market", "series.market"]),
            )
            source = self._resolve_value(
                item,
                raw_data,
                _candidate_paths(field_mapping.get("source"), ["source", "series.source"]),
            )
            obs_date_value = self._resolve_value(
                item,
                raw_data,
                _candidate_paths(
                    field_mapping.get("obs_date"),
                    [
                        "obs_date",
                        "date",
                        "observation_date",
                        "timestamp.last_update",
                        "timestamp.query_time",
                        "timestamp",
                    ],
                ),
            )
            value_value = self._resolve_value(
                item,
                raw_data,
                _candidate_paths(
                    field_mapping.get("value"),
                    ["value", "obs_value", "observation.value", "price.last", "last", "close"],
                ),
            )

            record = MacroObservationRecord(
                source_code=str(source_code).strip() if source_code is not None else "",
                obs_date=self._parse_obs_date(obs_date_value),
                value=self._parse_decimal(value_value),
                name=name,
                unit=unit,
                frequency=frequency,
                market=market,
                source=source,
                raw_data=item,
            )
            records.append(self._normalize_record(record))

        return records

    async def get_or_create_series(self, record: MacroObservationRecord) -> MacroSeries:
        """Get or create macro series by source_code."""
        stmt = select(MacroSeries).where(MacroSeries.source_code == record.source_code)
        result = await self.db.execute(stmt)
        series = result.scalar_one_or_none()

        if series:
            updated = False
            if record.name and series.name != record.name:
                series.name = record.name
                updated = True
            if record.unit and series.unit != record.unit:
                series.unit = record.unit
                updated = True
            if record.frequency and series.frequency != record.frequency:
                series.frequency = record.frequency
                updated = True
            if record.market and series.market != record.market:
                series.market = record.market
                updated = True
            if record.source and series.source != record.source:
                series.source = record.source
                updated = True
            if updated:
                series.updated_at = utc_now()
            return series

        series = MacroSeries(
            series_id=uuid7(),
            name=record.name or record.source_code,
            unit=record.unit,
            frequency=record.frequency,
            market=record.market,
            source_code=record.source_code,
            source=record.source,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db.add(series)
        await self.db.flush()
        return series

    async def upsert_observation(
        self,
        series_id: UUID,
        record: MacroObservationRecord,
        run_id: UUID,
    ) -> None:
        """Upsert macro observation."""
        stmt = insert(MacroObservation).values(
            id=uuid7(),
            series_id=series_id,
            obs_date=record.obs_date,
            value=record.value,
            source=record.source,
            asof_ts=utc_now(),
            run_id=run_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_macro_observation",
            set_={
                "value": stmt.excluded.value,
                "source": stmt.excluded.source,
                "asof_ts": stmt.excluded.asof_ts,
                "run_id": stmt.excluded.run_id,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await self.db.execute(stmt)

    async def process(self, raw_payload: dict, run_id: UUID) -> NormalizeResult:
        """Process macro payload and upsert into canonical storage."""
        result = NormalizeResult(run_id=run_id)

        try:
            await self.update_run_status(run_id, "processing", 0, 0, 0)

            mapped_records = self.map_fields(raw_payload)
            result.total_records = len(mapped_records)

            seen_keys: set[tuple[str, Optional[date]]] = set()

            for record in mapped_records:
                issues: list[DQIssueRecord] = []

                if not record.source_code:
                    issues.append(
                        DQIssueRecord(
                            issue_type="MISSING_SOURCE_CODE",
                            severity="error",
                            description="Missing macro series source_code",
                            raw_data=record.raw_data,
                        )
                    )
                if not record.obs_date:
                    issues.append(
                        DQIssueRecord(
                            issue_type="MISSING_OBS_DATE",
                            severity="error",
                            description="Missing observation date",
                            raw_data=record.raw_data,
                        )
                    )
                if record.value is None:
                    issues.append(
                        DQIssueRecord(
                            issue_type="MISSING_VALUE",
                            severity="warning",
                            description="Missing observation value",
                            raw_data=record.raw_data,
                        )
                    )

                key = (record.source_code, record.obs_date)
                if record.source_code and record.obs_date and key in seen_keys:
                    issues.append(
                        DQIssueRecord(
                            issue_type="DUPLICATE_KEY",
                            severity="error",
                            description="Duplicate series/obs_date in batch",
                            raw_data=record.raw_data,
                        )
                    )
                else:
                    if record.source_code and record.obs_date:
                        seen_keys.add(key)

                blocking_issues = [i for i in issues if i.severity == "error"]
                if blocking_issues:
                    for issue in issues:
                        await self.record_dq_issue(issue, run_id)
                        result.dq_issues.append(issue)
                    result.failed_records += 1
                    continue

                series = await self.get_or_create_series(record)

                for issue in issues:
                    await self.record_dq_issue(issue, run_id)
                    result.dq_issues.append(issue)

                await self.upsert_observation(series.series_id, record, run_id)
                result.success_records += 1

            status = "completed" if result.failed_records == 0 else "completed_with_errors"
            await self.update_run_status(
                run_id,
                status,
                result.total_records,
                result.success_records,
                result.failed_records,
            )
            await self.db.commit()

        except Exception as e:
            result.error_message = str(e)
            await self.update_run_status(run_id, "failed", 0, 0, 0, str(e))
            await self.db.commit()

        return result
