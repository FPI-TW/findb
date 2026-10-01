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

## 全市場正式環境排程

`findb-fetch-full-market` 是獨立的正式環境入口。`configs/full_market.production.v1.json`
固定保留 `desired_state=stopped`，staging 的 NASDAQ-100、TW50 與 minute pilot 排程維持原有範圍。
正式環境另以四個 provider process 執行；每個 process 只取得該 provider 的憑證，以及必要的
Source、Serve calendar 與 R2 憑證。TAIFEX 使用公開來源，不需要 Twelve Data key。

七個 feed 是 Twelve Data 的 US／HK equity EOD、FinLab 的 TW equity／ETF EOD、Shioaji 的
TW equity／ETF minute，以及 TAIFEX 的 TW futures EOD。正式環境首次 rollout 必須維持停止，
由 Admin 審核官方名單的第一個 baseline、既有帳號能力及 activation date，再設定
`full_market_<provider>_v1` control 為 running。Source 決定 published release、每份 plan 的
50-member parts、deadline 與 canonical coverage；Fetcher 不提供自行縮小範圍或修改 deadline 的參數。

```bash
# 不使用憑證、不呼叫 provider，只驗證正式環境完整範圍與停止預設
findb-fetch-full-market --provider twelve_data --config /app/configs/full_market.production.v1.json --check

# 四個 provider 必須共用同一個 SQLite volume；目錄 0700、檔案 0600，執行 UID/GID 10001
findb-fetch-full-market --provider twelve_data --state-path /var/lib/findb-full-market/state.sqlite3 --initialize-state
findb-fetch-full-market --provider twelve_data --state-path /var/lib/findb-full-market/state.sqlite3 --require-stopped

# 明確收集、持久化並提交官方 universe；不會自行 publish 或 activate
findb-fetch-full-market --provider twelve_data --state-path /var/lib/findb-full-market/state.sqlite3 --sync-universes

# 只在 DB control=running 且 Source feed 已啟用時執行；缺少 readiness 仍保持阻擋
findb-fetch-full-market --provider twelve_data --state-path /var/lib/findb-full-market/state.sqlite3 --run-forever
findb-fetch-full-market --provider twelve_data --state-path /var/lib/findb-full-market/state.sqlite3 --health
```

官方名單來源為 Nasdaq Trader 的兩份 symbol directories、HKEX `ListOfSecurities.xlsx`、
TWSE／TPEx 公司資料及 ISIN 的精確 ETF sections，以及 TAIFEX `DailyMarketReportFut`。
US 排除 ETF、OTC、preferred（含 ACT `$` suffix）、warrants、rights 等；普通股／ADR 分類仍有
歧義時保留 `classification_gap`。HK 股票代號保留五位前導零及官方交易幣別；Equity 中無法
證明為 Main Board／GEM 普通股的子分類保留缺口。TW 普通股以 ISIN 精確股票 section 與 CFI ES 分類為準，並以公司表交叉核對；CFI ED 的 TDR
（含四位代號）與 CFI EP 特別股排除，無法核實的分類保留缺口。ETF section
包含主動式、槓桿／反向與債券 ETF。官方日期在未來或欄位改變時拒絕生效，不以空名單替代。

Universe 的 official/provider 原始來源會先存入 R2，以 SHA-256 與不可變參考提交 Source。
名單與 mapping 改動超過 20 筆**或** 2% 時由 Backend 保留原 published release，新的 candidate
等待 Admin；Fetcher 不自行接受第一個 baseline 或重大變更。Running process 每日刷新官方名單，
失敗時保存阻擋原因並隔一小時重試。TW ISIN 原始檔可超過 8 MiB，因此全市場 process 必須設定
`CLOUDFLARE_R2_MAX_OBJECT_BYTES=16777216`；取得與 ZIP 解壓縮皆有明確上限。

Readiness 預設位於 `/var/lib/findb-full-market/readiness/<provider>.json`，可用
`--readiness-file` 指定。`configs/full_market.readiness.example.json` 刻意使用 unknown/null，
不能用它啟動擷取。核准檔必須有有效期限、完整 dataset scope、來源 evidence URL/checksum，
以及已驗證的每日／每分鐘 request、每日 bytes、單次 response bytes 與可達到的 request rate。
容量檢查納入 20% retry headroom；無法在 completion window 內處理完整範圍時，整個 provider
保持阻擋，不升級方案、不改成 subset。來源 API 的 activation/readiness 審核也必須通過。
`python -m findb_fetcher.readiness_probe` 可執行唯讀帳號觀察：最多三個 Twelve Data 請求，
僅輸出安全的 limits／permissions 或 unknown 原因，並寫入 0600 sanitized report。

Twelve Data 的 US／HK 共用同一個 durable account quota ledger；呼叫前交易式預留 daily/minute
requests 與 bytes；SQLite 同一 transaction 同時核發 account pacing permit，使用 UTC epoch
保存下次允許時間，跨 runtime、重啟與 US／HK 共用帳號生效，時鐘回退不會提早放行。
UTC 時間在取得 SQLite 寫入鎖後才讀取；pacing、daily/minute windows 與後續超額 bytes
使用同一個 reservation window，等待鎖跨分鐘或跨日不會使用舊時間核發 permit。
等待最多每次一秒重查停止控制與證據期限，沒有 60 秒截斷；FinLab SDK 完成或失敗後亦會
延後持久化 permit（包含 catalogue 呼叫）；429 cooldown 以 account scope 跨 UTC 日、runtime
與重啟保留，不預扣未來日期的 quota。已知 HTTP 429 在 body 驗證與超額 bytes 記帳前
先保存 cooldown；即使 body 過大、非 identity encoding、非 JSON 或記帳另有錯誤仍保留。
Catalogue 在已知 HTTP 429 的 body 驗證或讀取失敗時仍回報 typed rate-limited error，
HTTP 200 的 malformed body 保持原本的 source error 分類。
Twelve Data 的 time-series／catalogue
與 TAIFEX report 以 identity encoding 的原始 HTTP bytes 檢查 readiness 單次上限；
拒絕非 identity Content-Encoding、無效或超額 Content-Length，缺少 Content-Length 時
仍串流檢查 raw bytes。Twelve Data 另取環境設定上限較小值；已讀到的超額 bytes
保留在 daily ledger，拒絕回應不建立 catalogue evidence 或成功 raw object。
Shioaji 1.7.1 使用常駐、靜默隔離 child process，
每個 child 只登入一次、以 TSE／OTC catalogue 查找實際合約，IPC 僅接受有大小上限的 detached
JSON；detached catalogue／Kbars 及可觀察的 SDK usage delta 都必須符合單次上限，
SDK usage bytes 無法確認時拒絕交付。SDK 取得後才可觀察的超額 bytes 會加回 daily ledger，
即使已超過 budget 仍保存超額，不建立成功 prepared body，也不退款。FinLab 1.5.7 每個 dataset/date 首次取得五個 OHLCV
欄位，每個 SDK 呼叫分別預留 daily/minute requests 與 bytes，並依 readiness 的每秒速率等待；
每欄 detached table 各自檢查單次 response 上限，五欄 derived bundle 檢查五次預留的
aggregate 上限（加上固定 JSON wrapper），亦受既有 R2 object 上限限制。
FinLab detached table／bundle bytes 僅是可觀察的資料量下限，不能代表 SDK 實際 network bytes；
readiness 的 verified evidence 必須另行涵蓋 SDK 完整 response、login 與 cache 行為的網路用量，
未知或未驗證時維持 readiness 阻擋，runtime 不製造 network usage measurement。
成功 bundle 供各商品共用，不增加逐商品 provider 呼叫。
分鐘 quota 拒絕時保留已預留的 daily quota；部分欄位失敗不建立成功 bundle 或 no_data。
各商品獨立驗證，成功商品以 incremental EOD 交付。Provider 原始資料先存 R2，再保存
完整 prepared request bytes/checksum；重啟後重用原本文與已接受的 run receipt，不重新擷取。
Shioaji timeout、child 結束或 IPC 失敗時會清除並關閉失效 session，下一個可重試工作
重建同版本隔離 child；正常流程仍共用一次登入，停止控制會關閉 child。

TAIFEX 保留 `TX/MTX/TMF/TE/TF` 實際月／週代碼，排除含 `/` 的價差合約，保留一般／盤後及
官方 attribution date、各時段 volume；不推算 expiry date、不產生 continuous series，也不以
相鄰時段差額合成成交量。實際 W1–W5 或 F1–F5 週別均照來源保存。官方一般時段為 08:45–13:45，到期日 13:30，
盤後為 15:00–翌日 05:00；manifest 的 17:00 是報表擷取 trigger。盤後 `NULL` settlement/OI
保持 null；OHLC（含 close）與 volume 缺漏也不補造。有 settlement／OI（含明確 0）、
部分 OHLC 或正成交量的 observation 交付 canonical。只有所有行情欄位缺漏且官方 volume
明確為 0 才提交具 raw evidence 的 `no_trade`；volume 未知的空行情保持 blocked。
Catchup 使用 TAIFEX 官方 CSV
逐 product、逐日查詢並核對實際日期；latest API 不匹配目標日期時不冒充歷史結果。

只建立 activation date 起、Serve 完整 published calendar 中實際開市日的 plan；以當期 deadline
優先；最新計畫的 mapping/manual 缺口不會阻擋較早獨立工作，帳號 quota 耗盡或停止控制
才結束當輪 acquisition。每個日期先透過 Source scoped 日期查詢恢復既有 frozen plan，
即使同日新 release 已發布或本機 state 重建，仍保留原 release、members 與 work item identity。新增 catchup 使用
`universes?as_of=<date>` 對應當日生效名單。EOD 成功資料使用 incremental，minute 使用穩定
snapshot/sequence identity；每次 request 限單商品、15,000 rows 與小於 1 MiB 本文。
Source plan 的完成狀態必須經 canonical 查核，queued run 不能視為完成。

空 provider 回應、mapping gap、rate limit 與 source error 都屬 unresolved/blocked，不自動變成
no-data。僅具日期、商品、session、URL/checksum、觀察時間及實質來源片段的 halted/no-trade
證據可以完成 no-data。TAIFEX 明確零 volume 且無成交價會附官方 report 證據提交 no-trade；
其他未能提供來源證據的商品保持缺口。八次無法恢復的 acquisition/source failure、mapping gap
或 normalization failure 轉 manual，`--health` 保留 unresolved、late 及 manual-required，交由
營運人員核實處置，不跳過後將整份 plan 宣稱完成。
