# NORMALIZE KNOWLEDGE BASE

Apply root `AGENTS.md` first. This file adds rules only for `app/services/normalize/`.

## OVERVIEW

Market-specific normalization layer that maps source payloads to canonical records and writes canonical tables.

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Base flow/template | `base.py` | `map_fields`, validation, persistence, run status updates |
| Dataset routing entry | `app/services/ingestion.py` | `NORMALIZER_MAP` picks class by `dataset_key` |
| Crypto mapping | `crypto.py` | Legacy CryptoNormalizer + CryptoBloombergNormalizer (bloomberg direct, dataset: `crypto_bloomberg_eod`) |
| Equity and index mapping | `equity.py` + `usstock.py` | US/global stock + regional index handling |
| FX mapping | `fx.py` | FXBloombergNormalizer (bloomberg direct, dataset: `fx_bloomberg_eod`) |
| Futures mapping | `futures.py` | Contract + continuous futures logic; WTXBloombergNormalizer (dataset: `wtx_eod`, source=`bloomberg`) |
| FinLab direct | `finlab.py` | TW stocks/ETFs + WTX 近月 from FinLab (datasets: `tw_equity_eod`, `tw_etf_eod`, `wtx_eod`) |
| Macro mapping | `macro.py` | Series + observation normalization; MacroBloombergNormalizer (dataset: `macro_bloomberg_observation`) |
| DQ checks | `../dq/validators.py` | Error vs warning behavior |

## CONVENTIONS (LOCAL)

- New normalizers must subclass `BaseNormalizer` and implement `map_fields(raw_data)`.
- `dataset_key`, `asset_class`, and `market` must be explicit class attributes.
- Use config-driven extraction where possible (`field_mapping`, `data_path`, `identifier_field`).
- Normalize `source` values to stable lowercase provider strings (for example `bloomberg`).
- Trade timestamps must be parsed as UTC-aware values before persistence.
- Writes must be idempotent by key (`instrument_id + trade_date` style constraints).

## ANTI-PATTERNS (LOCAL)

- Duplicating mapping/parsing utilities already in `BaseNormalizer`.
- Writing canonical rows before DQ error evaluation completes.
- Coupling router/request logic into normalizer classes.
- Swallowing persistence exceptions without updating run status or DQ artifacts.
- Hardcoding market-specific symbols in unrelated normalizers.

## CHANGE CHECKLIST

- Add class in this directory and export in `__init__.py`.
- Register `dataset_key -> NormalizerClass` in `app/services/ingestion.py`.
- Add or extend tests in `tests/test_normalize.py` or market-specific test files.
- Verify upsert behavior and DQ outcomes for error and warning cases.
