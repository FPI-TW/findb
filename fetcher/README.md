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
- Versioned US Common Stock symbol universe與credit/record/date硬上限。
- 每個symbol獨立identity與delivery的bounded multi-symbol orchestration。
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
| `7` | Universe部分或全部symbol失敗，包括provider rate limit |

## 受治理symbol universe

Repository內第一版pilot universe位於
`configs/twelve_data_us_common_stocks.v1.json`，只包含AAPL、MSFT與NVDA三個NASDAQ
Common Stock。檔案是無secrets、嚴格欄位且versioned的執行輸入；修改symbols或limits
時必須經code review，不能從未驗證的runtime字串動態擴張。

Universe dry-run必須明確提供date window或`outputsize`：

```bash
uv run --env-file .env findb-fetch-twelve-data \
  --universe-file configs/twelve_data_us_common_stocks.v1.json \
  --start-date 2024-01-02 \
  --end-date 2024-01-06
```

正式送出並等待每個symbol的terminal state：

```bash
uv run --env-file .env findb-fetch-twelve-data \
  --universe-file configs/twelve_data_us_common_stocks.v1.json \
  --start-date 2024-01-02 \
  --end-date 2024-01-06 \
  --deliver \
  --wait
```

目前採順序、逐symbol呼叫Twelve Data與Source API，而不是把多個symbols合成同一
provider response或FinDB delivery。這保證每個symbol維持獨立request/idempotency
identity，單一mapping或delivery錯誤不會污染其它symbols。Provider 429、Source
401/403/429、整體deadline或transport/protocol failure會停止後續呼叫，剩餘項目標記
為`not_attempted`；一般單symbol provider/mapping錯誤則記錄partial failure並繼續。

V1 universe限制：

| 限制 | Pilot設定 | 程式絕對上限 |
| --- | ---: | ---: |
| 每次symbols | 3 | 5 |
| 每symbol records | 260 | 5000 |
| 每次總records | 780 | 10000 |
| Date span | 366天 | 3660天 |
| 每次credits | 3 | 5 |

Twelve Data目前將`/time_series`計為每symbol 1 credit；[batch query也仍按symbol計費](https://support.twelvedata.com/en/articles/5203360-batch-api-requests)。
Universe會在呼叫provider前驗證預估credits，且每次嘗試（包含失敗與429）都計入本次
執行的`credits_used`。此切片不含scheduler、persistent retry或checkpoint。

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
