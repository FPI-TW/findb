# FinDB Fetcher

獨立的 FinDB Source API delivery client基礎套件。它只依賴repository-level
versioned contracts，不import backend，也不持有FinDB DB、RabbitMQ或Admin權限。

目前提供：

- Contract manifest、checksum與JSON Schema驗證。
- Canonical request確定性序列化。
- 保持相同body與idempotency key的bounded retry。
- 只重試transport errors、429、502、503、504。
- Twelve Data `/time_series`日線client與`market_eod.v1` adapter。
- 由去識別化真實response fixture覆蓋的provider mapping與mock Source API整合測試。
- 安全的container readiness入口；不會自動抓取或送出資料。

尚未提供production scheduler、checkpoint、S3 raw storage或常駐fetch loop。

## 開發

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run black --check .
```

從repository root建置：

```bash
docker build -f fetcher/Dockerfile -t findb-fetcher:local .
```

## Twelve Data手動抓取

本機開發將API key放在被Git忽略的`fetcher/.env`，不要加入版控：

```bash
uv run --env-file .env findb-fetch-twelve-data \
  --symbol AAPL \
  --dataset-key us_equity_eod \
  --start-date 2024-01-02 \
  --end-date 2024-01-06
```

命令會呼叫Twelve Data、轉換並驗證contract，最後只將canonical request印到
stdout，不會送至Source API。若同時指定`start-date`與`end-date`，不要再指定
`outputsize`，避免查詢區間被截斷。

固定provider參數為`interval=1day`、`order=asc`、`format=JSON`與
`adjust=splits`。多筆資料轉為`backfill`，單筆資料轉為`incremental`；目前只接受
`Common Stock`。Adapter也會確認response symbol，以及有指定時的exchange，與原始
request一致後才建立contract。

| Twelve Data欄位 | `market_eod.v1` |
| --- | --- |
| `meta.symbol` | `source_symbol`，未指定時也作為canonical symbol |
| `meta.currency` | 每筆資料的`currency` |
| `values[].datetime` | `trade_date` |
| `open/high/low/close` | 同名decimal欄位 |
| `volume` | integer `volume` |
| exchange、MIC與日期區間 | stable request/idempotency identity |

去識別化response fixture位於
`tests/fixtures/twelve_data/aapl_1day_2024-01-02_2024-01-05.json`。Provider參數與
限制應以[Twelve Data文件](https://twelvedata.com/docs/introduction/overview)為準。

## Runtime設定

| 變數 | 必填 | 預設 | 用途 |
| --- | --- | --- | --- |
| `SOURCE_API_URL` | 是 | — | FinDB Source API origin，不含 `/api/v1/source/ingest` |
| `SOURCE_CLIENT_KEY` | 是 | — | Fetcher專用DB-backed source client key |
| `FETCHER_CONTRACTS_DIR` | 否 | `/app/contracts` | Contract manifest目錄 |
| `FETCHER_REQUEST_TIMEOUT_SECONDS` | 否 | `30` | 單次HTTP timeout |
| `FETCHER_MAX_ATTEMPTS` | 否 | `3` | 包含首次呼叫的最大attempt數 |
| `FETCHER_MAX_RETRY_AFTER_SECONDS` | 否 | `30` | Retry-After與backoff上限 |
| `TWELVE_DATA_API_KEY` | 是 | — | 固定資料來源Twelve Data的runtime secret |
| `TWELVE_DATA_BASE_URL` | 否 | `https://api.twelvedata.com` | Twelve Data HTTPS origin |
| `TWELVE_DATA_TIMEOUT_SECONDS` | 否 | `30` | Provider request timeout |

容器預設執行 `python -m findb_fetcher`。它只驗證runtime設定與contracts後退出，
不會呼叫provider或產生delivery。Scheduler會在後續切片加入。
