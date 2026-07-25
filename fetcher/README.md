# FinDB Fetcher

獨立的 FinDB Source API delivery client基礎套件。它只依賴repository-level
versioned contracts，不import backend，也不持有FinDB DB、RabbitMQ或Admin權限。

目前提供：

- Contract manifest、checksum與JSON Schema驗證。
- Canonical request確定性序列化。
- 保持相同body與idempotency key的bounded retry。
- 只重試transport errors、429、502、503、504。
- Twelve Data `/time_series`日線client與`market_eod.v1` adapter。
- 明確選用的`--deliver`與具整體deadline的`--wait`手動工作流。
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

本機開發將API key放在被Git忽略的`fetcher/.env`，不要加入版控。從
`fetcher/.env.example`複製時，`FETCHER_CONTRACTS_DIR=../contracts`適用於以下從
`fetcher/`執行的命令；container則固定使用`/app/contracts`：

```bash
uv run --env-file .env findb-fetch-twelve-data \
  --symbol AAPL \
  --dataset-key us_equity_eod \
  --start-date 2024-01-02 \
  --end-date 2024-01-06
```

命令預設為dry-run：它會呼叫Twelve Data、轉換並驗證contract，只把不含資料列的
bounded JSON摘要印到stdout，不會送至Source API。若同時指定`start-date`與
`end-date`，不要再指定`outputsize`，避免查詢區間被截斷。

明確指定`--deliver`才會送入Source API：

```bash
uv run --env-file .env findb-fetch-twelve-data \
  --symbol AAPL \
  --dataset-key us_equity_eod \
  --start-date 2024-01-02 \
  --end-date 2024-01-06 \
  --deliver
```

加上`--wait`會使用同一組Fetcher Source client credential輪詢run狀態，直到
`completed`、`completed_with_errors`或`failed`。整體deadline從delivery前開始，
包含POST、retry、backoff與status polling：

```bash
uv run --env-file .env findb-fetch-twelve-data \
  --symbol AAPL \
  --dataset-key us_equity_eod \
  --start-date 2024-01-02 \
  --end-date 2024-01-06 \
  --deliver \
  --wait \
  --wait-timeout-seconds 3600 \
  --poll-interval-seconds 2
```

`--wait`必須搭配`--deliver`。Wait timeout預設3600秒、上限7200秒；poll interval
預設2秒、上限60秒，且不得大於wait timeout。stdout摘要限制為2048 bytes，只包含
dataset/schema、request/idempotency identity、資料日期與筆數，以及delivery後的
attempt/run identity與terminal counts；不輸出API key、完整payload或任意Source
response body。

CLI exit code：

| Code | 意義 |
| --- | --- |
| `0` | dry-run驗證成功、delivery accepted/duplicate，或run為`completed` |
| `2` | CLI參數、provider、runtime config或本機contract錯誤 |
| `3` | Source永久拒絕，例如auth、contract rejection或idempotency conflict |
| `4` | Transport retry耗盡、response超限/格式錯誤或protocol identity不一致 |
| `5` | `--wait`整體deadline到期 |
| `6` | Run終止於`failed`或`completed_with_errors` |

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
