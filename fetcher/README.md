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
- Fetcher-owned SQLite scheduler state、persistent retry、lease recovery與逐symbol checkpoint。
- FinLab `2330`、`2317` reviewed pilot 的dataset-level durable scheduler與完整
  two-row canonical成功閘門。
- Shioaji `2330`、`0050`、`0056`、`006201` reviewed pilot 的同日
  `14:30`–`17:00 Asia/Taipei` production scheduler。
- 安全的one-shot scheduler，以及需明確選用的常駐poll loop。
- Provider raw artifact先寫入Fetcher-owned Cloudflare R2，delivery攜帶immutable ref與SHA-256；
  Twelve Data保存exact HTTP bytes，SDK providers保存deterministic detached snapshots。
- 由去識別化真實response fixture覆蓋的provider mapping與mock Source API整合測試。
- 安全的container readiness與scheduler preflight入口；不會自動抓取或送出資料。

Backend migration會以`stopped`建立三筆scheduler-control資料；Fetcher CD只負責讓三個
`--run-forever` container保持常駐，不再控制desired state。每個runtime以對應的固定
scheduler key輪詢Source control endpoint，並只在owner透過Dashboard授權`running`後
建立provider cycle。每個provider使用獨立immutable image、Source client key、container
與durable SQLite state，不能互相共用credential或state。
R2 bucket與API
token已由外部提供；raw object lifecycle為30天，bucket lock為7天，兩者由Cloudflare
R2管理而非Fetcher scheduler。

### Staging execution boundary

Staging只驗證完整資料流與故障處理，不承載完整資料集。預設data-producing profile
固定使用committed AAPL、MSFT、NVDA universe與scheduler `outputsize=20`；fresh cycle
最多3個symbols、60筆provider rows。Universe內的260筆per-symbol與780筆total hard
limits是程式安全上限，不是staging載入授權。

不得在staging執行完整歷史backfill、完整universe導入或擴大symbol、日期、record
caps；例外必須依
[staging data policy](../docs/operations/deployment.md#staging-data-policy)
針對具名run另行核准。常駐 `--run-forever` 只代表排程方式，不授權大量導入。
Bounded acceptance完成後預設停止data-producing scheduler；只有具名觀察窗口可保持
運行，且仍須遵守相同caps。

## 開發

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

從repository root建置：

```bash
docker build -f fetcher/Dockerfile -t findb-fetcher:local .
```

## FinLab staging acquisition smoke

FinLab 是隔離的 optional runtime，通用 Fetcher image 不安裝其 SDK。只為驗證部署端
`FINLAB_API_TOKEN`能讀取資料時，建置獨立 image：

```bash
docker build -f fetcher/Dockerfile.finlab -t findb-fetcher-finlab:local .
docker run --rm --read-only \
  --tmpfs /home/fetcher:uid=10001,gid=10001,mode=0700 \
  --env FINLAB_API_TOKEN \
  findb-fetcher-finlab:local \
  findb-fetch-finlab-smoke --target-date 2026-07-29 --symbols 2330,2317
```

此命令固定使用 `finlab==1.5.7` 和 `price:收盤價`，只接受已審核的 `2330`、`2317`
（最多兩檔）及明確 ISO 日期。stdout 是不含價格、token、cache path 或 provider error
的 bounded JSON，只包含狀態、SDK 版本、日期、symbols、筆數與內容 checksum。它不會
寫 R2、呼叫 Source API、建立 scheduler state 或啟用排程。

遠端只能透過 Fetcher CD 的手動 `workflow_dispatch` 設定 `run_finlab_smoke=true` 觸發，
而且固定在 `staging-fetcher` Environment 執行；push 和一般 Fetcher deployment 永遠
不會呼叫 FinLab。CD 建立 ephemeral、read-only、non-root container，僅注入
`FINLAB_API_TOKEN`，並把 SDK cache 掛載至其專用目錄。不可把 Source、R2 或 Twelve Data
credentials 加到此 smoke container。

一次 acquisition 的硬上限為 300 秒，涵蓋 cold cache 的 SDK metadata/data 初始化；逾時會
終止隔離 child 並只輸出 generic `acquisition_failed`，不重試或啟用 scheduler。

## FinLab durable reviewed pilot

FinLab production pilot固定在`tw_1430`抓取`2330`與`2317`的五個OHLCV datasets，組成
單一`market_eod.v1` `full_snapshot` delivery。每一輪先保存deterministic SDK acquisition
bundle至raw R2，再保存prepared Source request；重啟後直接重送相同request，不會再次
呼叫SDK。只有Source run為`completed`且total/success均為2、failed為0時才推進checkpoint。

本機preflight與常駐執行：

```bash
uv run --env-file .env findb-fetch-finlab-scheduler --check
uv run --env-file .env findb-fetch-finlab-scheduler --run-forever
```

Preflight會驗證已啟用的`tw_1430` feed、exact two-symbol reviewed manifest、runtime config、
contracts與state，但不建立provider、Source、calendar或R2 client。Production state預設為
`/var/lib/findb-finlab-fetcher/state.sqlite3`；此pilot不是FinLab全市場授權。

## Shioaji simulation acquisition smoke

`shioaji==1.7.1` 是 optional dependency。`findb-fetch-shioaji-smoke` 只接受 staging
allowlist 的單一 `2330` 與明確日期（最近 31 天內），以 `simulation=True` 取得一次 Kbars。
它不會 delivery、寫 DB/R2 或啟用 scheduler；SDK 工作會在輸出靜音的 bounded child 執行，
並保證登出。成功 stdout 僅包含版本、日期、symbol、筆數與 checksum。Shioaji `usage()` 的
bytes/connections 不會被當成 request count；contract 中的 usage 是本地單調 request-attempt
計數。Kbars `ts` 依 Asia/Taipei wall-clock 的 right-labelled bar end 轉換；13:30 close-auction
仍是已知語意限制，須待 contract 決策後才可特例化。

## Shioaji Taiwan-minute staging coordinator

`SHIOAJI_SIMULATION` controls the isolated staging child and is strict:
an omitted value defaults to `true`, and an explicit value must be exactly
`true`; every other value fails closed without echoing its content. This
remains read-only Kbars acquisition and never invokes order APIs.

The reviewed staging symbols `2330`, `0050`, `0056`, and `006201` use the
public `BaseContract(security_type="STK", exchange="TSE", code=..., region="TW")`
route. It is a live-proven simulation workaround for Shioaji catalog
initialization timeouts, so these four symbols bypass catalog access entirely.
Other symbols retain the existing catalog path; this route does not infer an
exchange for arbitrary symbols.

`findb-fetch-shioaji-staging --check` validates only the committed four-symbol
manifest and is fully offline. Phase 4 credentialed preflight uses a fresh,
private `FETCHER_SHIOAJI_STAGING_STATE_PATH`, an explicit known prior trading
date within 31 days, and must run before 17:00 Asia/Taipei. First run
`--preflight` (only reviewed symbol `2330`); only after that command succeeds,
run the normal validate-only command with the same state path to acquire the
remaining three symbols. The saved 2330 snapshot is reused, so it is never
re-fetched. Never use `--deliver` for this Phase 4 flow.

The same state records a durable preflight-success marker only after 2330 has
both been acquired and passed local contract validation. A normal four-symbol
validate-only run without that marker fails before any provider call.

The state is bound to the Taipei execution date and cannot resume on a later
day. Stored bytes are an SDK-detached `shioaji_sdk_acquisition_snapshot.v1`,
not exact HTTP provider response bytes. This remains staging-only: it does not
activate a dataset, schedule work, authorize production/backfill use, or create
R2/Source clients during preflight or normal validation.

## Shioaji durable reviewed pilot

Production pilot固定使用`2330` equity與`0050`、`0056`、`006201` ETF。Scheduler只在
published TW calendar標示當日開市時，於`14:30`後取得同一Taipei trade date；`17:00`
後停止provider重試，而且不會在隔日自動補抓。Acquisition attempt、rolling rate-limit、
SDK-detached snapshot、raw R2 intent/object、prepared Source request與terminal結果均保存在
production專用SQLite state。只有Source `completed`且record counts完全一致才視為成功。

```bash
uv run --env-file .env findb-fetch-shioaji-scheduler --check
uv run --env-file .env findb-fetch-shioaji-scheduler --run-forever
```

Production runtime強制`SHIOAJI_SIMULATION=true`並要求獨立Shioaji data-only credentials、Source key、
R2 binding、writable provider cache與`/var/lib/findb-shioaji-fetcher/state.sqlite3`。不可把staging state搬入production。
這是四檔reviewed pilot；完整市場universe、跨sequence publication barrier、archive與
Serve minute仍屬下一階段。

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

所有`--deliver`路徑會先要求Fetcher raw R2設定並成功保存provider response；dry-run
不讀S3設定，也不建立AWS client。Raw upload失敗時不會呼叫Source API。

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
執行的`credits_used`。

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

## Twelve Data durable scheduler

Scheduler設定位於
`configs/twelve_data_us_common_stocks_daily.v1.json`，固定引用同目錄的versioned
universe。預設每日`22:00 UTC`（美股收盤後）建立一次最近工作日的工作，每個symbol
獨立保存狀態。
安全預設是只執行一個due cycle後退出：

```bash
uv run --env-file .env findb-fetch-scheduler \
  --state-path .state/scheduler.sqlite3
```

只有明確指定`--run-forever`才會進入poll loop：

```bash
uv run --env-file .env findb-fetch-scheduler \
  --state-path .state/scheduler.sqlite3 \
  --run-forever
```

部署前可執行不觸發外部呼叫的preflight。它驗證schedule、universe bounds、runtime
config、contracts及SQLite path/schema與`PRAGMA quick_check`，但不建立provider、
Source或R2 client，也不enqueue工作。宣告目前schema version但缺少table、column、
必要index或constraint的state會fail closed，不會被preflight靜默重建：

```bash
uv run --env-file .env findb-fetch-scheduler \
  --state-path .state/scheduler.sqlite3 \
  --check
```

SQLite檔屬於Fetcher自己的runtime state，不是FinDB DB，也不包含provider或Source
credentials。Production必須將`/var/lib/findb-fetcher`掛載至單一writer使用的durable
volume；CD以UID/GID `10001:10001`及directory mode `0700`驗證此mount，不能把
container writable layer當checkpoint。狀態包含：

- 每個schedule date與symbol的`pending`、`running`、`retry_wait`、`completed`或
  `failed`狀態。
- Attempt count、bounded exponential retry時間、lease與上次安全outcome。
- Source attempt/run identity，以及只有terminal `completed`才會推進的trade-date
  checkpoint。
- Delivery尚未成功時暫存已驗證的canonical request；跨程序delivery retry直接重用
  相同body與idempotency key，完成或terminal failure後即清除payload。

首次執行使用設定內的bounded `outputsize` bootstrap。已有checkpoint後改用
`checkpoint + 1 day`到schedule date的完整bounded日期區間；若中斷跨度超過universe
的date-span硬上限，會以`checkpoint_gap_exceeded`停止，不能靜默跳過資料。重啟時，
逾期的`running` lease會回到retry queue；同一scheduled job採確定性job key，重跑
Source delivery仍受既有idempotency保護。

Transient provider/Source 429、5xx、transport及wait timeout會保存在SQLite等待下次
attempt。若fetch尚未成功，下次attempt才重新呼叫provider並計入credit；若canonical
request已保存，delivery retry不重新抓取、`credits_used`也不增加。Mapping、
contract、auth、Source protocol及normalization terminal failure不自動重試；下一個
schedule date仍可重新嘗試該symbol。排程不內建交易所假日日曆，但週末會回退到最近
星期五；假日無資料由provider結果明確記錄，下一個工作日仍從未前進的checkpoint
接續。

## Provider raw storage（Cloudflare R2）

Twelve Data以streaming hard cap讀取response，要求`Accept-Encoding: identity`並拒絕
其它content encoding；R2保存HTTP client實際收到的exact bytes。FinLab保存
deterministic SDK dataset bundle，Shioaji保存SDK-detached acquisition snapshot；後兩者
不宣稱是provider HTTP bytes，但同樣在Source delivery前完成raw-first persistence與
SHA-256 provenance。

Raw-enabled執行順序固定為：

1. Bounded provider fetch。
2. 將exact bytes寫入Fetcher-owned Cloudflare R2。
3. Provider mapping並加入`source_raw_ref`與`source_raw_sha256`。
4. Contract validation。
5. 保存scheduler prepared request。
6. Source delivery與terminal wait。

Object key依dataset、source symbol hash與content SHA-256確定性產生；delivery只帶不含
credentials或query string的`r2://account-id/bucket/key`。R2 S3-compatible PutObject
明確帶`ContentLength`、`ChecksumSHA256`、content type與metadata。Endpoint只由已驗證
的Cloudflare account ID組成，不接受任意URL；credentials使用bucket-scoped R2 API
token或短效credentials。R2會自動以AES-256加密所有object及metadata，因此程式不傳送
R2不支援的AWS SSE/KMS headers。

`source_raw_ref`與`source_raw_sha256`必須成對存在，raw provenance也會納入
request/idempotency identity。因此相同raw artifact重試維持相同identity；即使
canonical rows相同，只要provider exact bytes改變，就會建立不同identity，避免Source
把同key/different payload判為409 collision。

Scheduler在delivery retry時直接重用SQLite內的prepared canonical request，不重新抓取
或上傳。R2 transient 429/5xx/transport failure可進入persistent retry；auth、bucket
或其它deterministic 4xx會terminal fail且不推進checkpoint。舊prepared state若缺
raw provenance會fail closed，不會靜默送出。

Scheduler one-shot exit code：

| Code | 意義 |
| --- | --- |
| `0` | 本次schedule的所有symbol均已完成 |
| `2` | Schedule、universe、runtime secrets、contract或state設定錯誤 |
| `8` | 至少一項等待persistent retry或仍pending |
| `9` | 至少一項進入terminal scheduler failure |

## Runtime設定

| 變數 | 必填 | 預設 | 用途 |
| --- | --- | --- | --- |
| `SOURCE_API_URL` | 是 | — | FinDB Source API HTTPS origin；只允許root/trailing slash與有效optional port，不含credentials、path、query或fragment |
| `SOURCE_CLIENT_KEY` | 是 | — | 當前provider runtime專用DB-backed Source client key；三個provider不得共用 |
| `FETCHER_CONTRACTS_DIR` | 否 | `/app/contracts` | Contract manifest目錄 |
| `FETCHER_REQUEST_TIMEOUT_SECONDS` | 否 | `30` | 單次HTTP timeout |
| `FETCHER_MAX_ATTEMPTS` | 否 | `3` | 包含首次呼叫的最大attempt數 |
| `FETCHER_MAX_RETRY_AFTER_SECONDS` | 否 | `30` | Retry-After與backoff上限 |
| `FETCHER_SCHEDULER_CONTROL_POLL_SECONDS` | 否 | `30` | scheduler control輪詢與heartbeat間隔；限制為`1`–`30`秒，確保90秒stale門檻至少涵蓋三次heartbeat |
| `FETCHER_SCHEDULE_FILE` | 否 | `/app/configs/twelve_data_us_common_stocks_daily.v1.json` | Versioned scheduler設定 |
| `FETCHER_STATE_PATH` | 否 | `/var/lib/findb-fetcher/state.sqlite3` | Fetcher-owned durable SQLite state |
| `FETCHER_FINLAB_STATE_PATH` | 否 | `/var/lib/findb-finlab-fetcher/state.sqlite3` | FinLab專用durable SQLite state |
| `FINLAB_API_TOKEN` | FinLab是 | — | FinLab headless SDK token |
| `FETCHER_SHIOAJI_STATE_PATH` | 否 | `/var/lib/findb-shioaji-fetcher/state.sqlite3` | Shioaji production專用durable SQLite state |
| `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` | Shioaji是 | — | Production Shioaji帳號；不得與staging共用 |
| `SHIOAJI_SIMULATION` | Shioaji是 | `true` | 所有環境只接受simulation mode；明確的`false`會fail closed |
| `TWELVE_DATA_API_KEY` | 是 | — | 固定資料來源Twelve Data的runtime secret |
| `TWELVE_DATA_BASE_URL` | 否 | `https://api.twelvedata.com` | Twelve Data HTTPS origin |
| `TWELVE_DATA_TIMEOUT_SECONDS` | 否 | `30` | Provider request timeout |
| `TWELVE_DATA_MAX_RESPONSE_BYTES` | 否 | `8388608` | Provider response streaming上限，程式硬上限16 MiB |
| `CLOUDFLARE_R2_ACCOUNT_ID` | Delivery/scheduler是 | — | 32字元Cloudflare account ID，用來建立固定R2 endpoint |
| `CLOUDFLARE_R2_RAW_BUCKET` | Delivery/scheduler是 | — | 所有環境唯一支援的Fetcher-owned private raw bucket名稱 |
| `CLOUDFLARE_R2_RAW_ACCESS_KEY_ID` | Delivery/scheduler是 | — | Raw bucket專屬的Object Read & Write R2 S3 API access key |
| `CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY` | Delivery/scheduler是 | — | Raw bucket專屬的Object Read & Write R2 S3 API secret |
| `CLOUDFLARE_R2_RAW_SESSION_TOKEN` | 否 | — | 使用Raw bucket temporary credentials時設定 |
| `CLOUDFLARE_R2_MAX_OBJECT_BYTES` | 否 | `8388608` | Raw object上限，程式硬上限16 MiB |

通用、FinLab與Shioaji images各自提供安全的預設命令；部署時才以對應scheduler
`--run-forever`覆蓋並掛載provider專用durable state。Rollout先以同一image、mount及
runtime環境執行`--check`，通過後才替換舊container。候選container需持續存活且未重啟
才會取得stable名稱，否則移除候選並復原舊container。若程序突然SIGTERM，執行中的job
會留到lease到期後由scheduler回收，而非立即重派。
