# FinDB Fetcher

獨立的 FinDB Source API delivery client。Fetcher 只依賴 repository-level versioned
contracts，不 import backend，也不持有 FinDB DB、RabbitMQ 或 Admin 權限。

## Staging active feeds

目前 staging 只啟用四個 provider/dataset 配對：

| Provider | Dataset | 範圍 |
| --- | --- | --- |
| `twelve_data` | `us_equity_eod` | reviewed bounded US equity universe |
| `finlab` | `tw_equity_eod` | reviewed bounded TW equity universe |
| `shioaji` | `tw_equity_minute` | `2330` reviewed pilot |
| `shioaji` | `tw_etf_minute` | `0050`、`0056`、`006201` reviewed pilot |

Canonical/Serve read model 仍可保留歷史或預留資料域，但不代表那些資料域目前有
active provider feed。舊 provider、direct/market ingest route 與 futures contract feed
不在 staging；若日後需要，必須另案建立完整新版 contract、dataset registry、normalizer、
DQ、Serve read model 與 staging 驗收。

目前提供：

- Contract manifest、checksum 與 JSON Schema 驗證。
- Canonical request 確定性序列化與維持 body/idempotency key 的 bounded retry。
- 只重試 transport errors、429、502、503、504。
- Twelve Data `/time_series` 日線 client 與 `market_eod.v1` adapter。
- FinLab `tw_equity_eod` durable scheduler 與 Shioaji `tw_equity_minute`／
  `tw_etf_minute` same-day scheduler。
- 每個 provider 使用獨立 Source client key、container 與 durable SQLite state；raw artifact
  先寫入 Fetcher-owned Cloudflare R2，delivery 攜帶 immutable ref 與 SHA-256。
- 去識別化 provider fixture、mapping、mock Source API、readiness 與 scheduler preflight。

Fetcher 只能透過 `POST /api/v1/source/ingest` 傳送 provider-neutral contract。任何
retired route、provider-specific route 或不在上述表格的 dataset 都會 fail closed，不能
以 fallback 方式恢復。

### Staging execution boundary

Staging 只驗證完整資料流與故障處理，不承載完整資料集。預設 data-producing profile
固定使用 AAPL、MSFT、NVDA 與 scheduler `outputsize=20`；fresh cycle 最多 3 個 symbols、
60 筆 provider rows。不得在 staging 執行完整歷史 backfill、完整 universe 導入或擴大
symbol、日期、record caps；例外須依[staging data policy](../docs/operations/deployment.md#staging-data-policy)
針對具名 run 另行核准。

## 開發

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

從 repository root 建置：

```bash
docker build -f fetcher/Dockerfile -t findb-fetcher:local .
```

## FinLab staging smoke

FinLab 是隔離的 optional runtime；通用 Fetcher image 不安裝其 SDK。只為驗證 staging
端 `FINLAB_API_TOKEN` 能讀取資料時，建置獨立 image：

```bash
docker build -f fetcher/Dockerfile.finlab -t findb-fetcher-finlab:local .
docker run --rm --read-only \
  --tmpfs /home/fetcher:uid=10001,gid=10001,mode=0700 \
  --env FINLAB_API_TOKEN \
  findb-fetcher-finlab:local \
  findb-fetch-finlab-smoke --target-date 2026-07-29 --symbols 2330,2317
```

命令固定使用 `finlab==1.5.7` 與 `price:收盤價`，只接受已審核的 2330、2317（最多兩檔）
及明確 ISO 日期；不寫 R2、不呼叫 Source API、不建立 scheduler state。遠端 smoke 只可
在 `staging-fetcher` Environment 手動觸發，不能把 Source、R2 或其他 provider credential
注入 smoke container。

## FinLab scheduler

本機 preflight 與常駐執行：

```bash
uv run --env-file .env findb-fetch-finlab-scheduler --check
uv run --env-file .env findb-fetch-finlab-scheduler --run-forever
```

Preflight 驗證 `tw_equity_eod` feed、reviewed manifest、runtime config、contracts 與
SQLite state；只有 owner 透過 Dashboard 將 DB desired state 設為 `running` 後才建立
provider cycle。每輪先保存 deterministic SDK bundle 與 prepared Source request；重啟後
重用相同 request，只有 Source `completed` 且 record count 一致才推進 checkpoint。

## Shioaji staging smoke 與 scheduler

`shioaji==1.7.1` 是 optional dependency。Smoke 只接受 staging allowlist 的單一 2330
與明確日期（最近 31 天內），以 `simulation=True` 取得一次 Kbars；不 delivery、不寫 DB/R2、
不啟用 scheduler。時間戳以 `Asia/Taipei` wall-clock right-labelled bar end 轉換。

```bash
uv run --env-file .env findb-fetch-shioaji-smoke --symbol 2330 --target-date 2026-07-29
uv run --env-file .env findb-fetch-shioaji-scheduler --check
uv run --env-file .env findb-fetch-shioaji-scheduler --run-forever
```

Shioaji scheduler 固定服務 `tw_equity_minute` 與 `tw_etf_minute`；使用獨立 Shioaji
credentials、Source key、R2 binding、provider cache 與
`/var/lib/findb-shioaji-fetcher/state.sqlite3`。同一 snapshot 的 sequences 必須完整成功
才解除 missing alert；不跨日自動補抓。

## Twelve Data 手動抓取

本機開發將 API key 放在被 Git 忽略的 `fetcher/.env`。從 `fetcher/.env.example` 複製時，
`FETCHER_CONTRACTS_DIR=../contracts` 適用於從 `fetcher/` 執行的命令；container 固定使用
`/app/contracts`。

```bash
uv run --env-file .env findb-fetch-twelve-data \
  --symbol AAPL \
  --dataset-key us_equity_eod \
  --start-date 2024-01-02 \
  --end-date 2024-01-06
```

預設為 dry-run：呼叫 Twelve Data、轉換並驗證 contract，只輸出不含資料列的 bounded JSON，
不送 Source API。明確指定 `--deliver` 才會 delivery；加上 `--wait` 使用同一 Source key
輪詢 terminal state：

```bash
uv run --env-file .env findb-fetch-twelve-data \
  --symbol AAPL \
  --dataset-key us_equity_eod \
  --start-date 2024-01-02 \
  --end-date 2024-01-06 \
  --deliver --wait --wait-timeout-seconds 3600
```

`--deliver` 會先完成 raw R2 persistence；raw upload 失敗時不呼叫 Source。CLI 不輸出 API
key、完整 payload 或任意 Source response body。exit code 以 CLI `--help` 與程式碼為準。

### Twelve Data reviewed universe

`configs/twelve_data_us_common_stocks.v1.json` 只包含 AAPL、MSFT、NVDA。修改 symbols 或
limits 必須經 code review，不得從 runtime 字串動態擴張。每個 symbol 保持獨立 request 與
idempotency identity；單一 mapping/delivery 錯誤不污染其它 symbols。Universe 必須遵守
每次 3 symbols、每 symbol 260 records、總計 780 records、366 天與 3 credits 的 reviewed
limits（程式絕對上限更高但不是操作授權）。

主要 mapping：`meta.symbol`→`source_symbol`、`meta.currency`→`currency`、
`values[].datetime`→`trade_date`、`open/high/low/close/volume`→同名 canonical 欄位。
去識別化 fixture 位於 `tests/fixtures/twelve_data/`。

## Scheduler state 與 control boundary

Fetcher-owned SQLite 不是 FinDB DB，也不包含 provider 或 Source credentials。SQLite 保存
schedule date/symbol 狀態、retry/lease、Source attempt/run identity、prepared request 與
terminal checkpoint；delivery retry 直接重用相同 body 與 idempotency key。DB scheduler
control endpoint 是啟停唯一權威，失聯或 mapping 不一致時 fail closed。

三個常駐 scheduler container 使用 `--run-forever`，分別服務 Twelve Data、FinLab、Shioaji；
stopped 時保持 idle，不建立 provider cycle。state directory 應由 staging deployment 以
UID/GID `10001:10001`、mode `0700` 的 durable volume 掛載。

常駐CLI會將container runtime的`SIGTERM`及互動式`SIGINT`轉成shared stop event。Idle loop
會立即以exit `0`結束；若signal落在final running preflight後，runtime不會跨入新的provider
cycle；已開始的cycle仍會完成terminal report再結束。Deployment要求stable container在30秒
grace period內exit `0`，exit `137`一律視為no-go並恢復原stable。

## Provider raw storage（Cloudflare R2）

Twelve Data 保存 exact HTTP bytes；FinLab 保存 deterministic SDK bundle；Shioaji 保存
SDK-detached acquisition snapshot。三者都在 Source delivery 前完成 raw-first persistence，
並以 `source_raw_ref` 與 `source_raw_sha256` 成對傳遞。

執行順序固定為：bounded provider fetch → R2 raw write → mapping/contract validation →
prepared request → Source delivery/terminal wait。R2 object key 由 dataset、source symbol
hash 與 content SHA-256 確定性產生；只傳 credential-free `r2://account-id/bucket/key`。
Raw object lifecycle 為 30 天、bucket lock 為 7 天，由 Cloudflare R2 管理。

## Runtime 設定

| 變數 | 必填 | 用途 |
| --- | --- | --- |
| `SOURCE_API_URL` | 是 | FinDB Source API HTTPS origin |
| `SOURCE_CLIENT_KEY` | 是 | 當前 provider 專用 DB-backed Source client key |
| `FETCHER_CONTRACTS_DIR` | 否 | Contract manifest 目錄（container 預設 `/app/contracts`） |
| `FETCHER_STATE_PATH` | 否 | Twelve Data durable SQLite state |
| `FETCHER_FINLAB_STATE_PATH` | 否 | FinLab durable SQLite state |
| `FETCHER_SHIOAJI_STATE_PATH` | 否 | Shioaji durable SQLite state |
| `FINLAB_API_TOKEN` | FinLab 是 | FinLab headless SDK token |
| `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` | Shioaji 是 | Staging Shioaji credentials |
| `SHIOAJI_SIMULATION` | Shioaji 是 | 預設 `true`；明確 `false` fail closed |
| `TWELVE_DATA_API_KEY` | Twelve Data 是 | Twelve Data runtime secret |
| `CLOUDFLARE_R2_ACCOUNT_ID` | delivery/scheduler 是 | Cloudflare account ID |
| `CLOUDFLARE_R2_RAW_BUCKET` | delivery/scheduler 是 | Fetcher-owned private raw bucket |
| `CLOUDFLARE_R2_RAW_ACCESS_KEY_ID` | delivery/scheduler 是 | Raw bucket R2 access key |
| `CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY` | delivery/scheduler 是 | Raw bucket R2 secret |

每個 provider 使用自己的 Source key、credentials、container 與 state。Secrets 只從
`staging-fetcher` Environment/instance role 載入，不進 log、contract、raw payload 或
Dashboard client bundle。
