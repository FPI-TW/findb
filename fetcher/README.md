# FinDB Fetcher

獨立的 FinDB Source API delivery client。Fetcher 只依賴 repository-level versioned
contracts，不 import backend，也不持有 FinDB DB、RabbitMQ 或 Admin 權限。

環境身份統一由 `APP_ENVIRONMENT=local|staging|production` 載入；
`FULL_MARKET_ENABLED` 控制該環境的 Full market opt-in。既有 config/manifest 的
`deployment_target` 欄位維持原契約；舊 immutable accepted replay 的等值環境映射與
明確 legacy receipt 檢查見[部署維運規則](../docs/operations/deployment.md)。

## Staging active feeds

staging 的四個 providers 以五個 provider/dataset 配對進行 functional pilot：

| Provider | Dataset | 範圍 |
| --- | --- | --- |
| `twelve_data` | `us_equity_eod` | `AAPL`、`MSFT` 單日完整日 K |
| `finlab` | `tw_equity_eod` | `2330`、`2317` 單日完整日 K |
| `shioaji` | `tw_equity_minute` | `2330` reviewed pilot |
| `shioaji` | `tw_etf_minute` | `0050` 完整同日分鐘 K |
| `taifex` | `tw_futures_eod` | `TX`、`MTX` 各一個 source-observed 近月契約，regular／after_hours |

Canonical/Serve read model 仍可保留歷史或預留資料域，但不代表那些資料域目前有
active provider feed。HK、TW ETF EOD 不為 provider coverage 擴張；環境 opt-in Full market
須 Owner baseline publish 與完整 calendar，之後 Owner 在 Full market tab 手動啟動。

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
由 `configs/staging_provider_pilots.v1.json` 綁定每個 provider 1–2 商品與
最新已完成交易日，不補歷史缺口。Twelve 最多 2 requests／2 rows／單日；FinLab 一批 2 rows；
Shioaji 各 dataset 一筆 sequence（sequence_count=1），保留完整同日分鐘 bars；TAIFEX 每日期
最多 6 次單商品單日報表 requests（3 attempts）、每 response 8 MiB、4 canonical session rows。不得在 staging 執行完整歷史 backfill、完整 universe 導入或擴大
symbol、日期、record caps；例外須依[staging data policy](../docs/operations/deployment.md#staging-data-policy)
針對具名 run 另行核准。

## 開發

以下本機命令皆從 `fetcher/` 執行。首次設定時，將 `.env.example` 複製為被 Git 忽略的
`.env`，再填入當前 provider 的 credentials 與專用 Source key：

```bash
cd fetcher
cp .env.example .env
uv sync --frozen
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

本機 `.env` 的 `FETCHER_ACCOUNT_STATE_PATH=.state/account/governor.sqlite3` 解析為
`fetcher/.state/account/governor.sqlite3`；一般使用者可建立此目錄。所有 providers 的
Pilot、Full、maintenance 與 historical 命令均須載入同一份 `.env`、使用此共用 durable
ledger，即使 `FULL_MARKET_ENABLED=false` 也相同。Checkpoint 可各自隔離；切換 profile、
重啟或更新 credentials 不得清除 account pins、既有用量、unknown liability 或 cooldown。
若從其他工作目錄啟動，將 `FETCHER_ACCOUNT_STATE_PATH` 設為該既有檔案的絕對路徑，
並一併調整 contracts／schedule／checkpoint 的相對路徑；不要只沿用同一個相對路徑字串。

既有 `.env` 不會因更新範本而自動新增此設定。升級前先停止所有使用該 ledger 的 consumers，
確認曾使用的 governor 路徑（包括舊 `/private/tmp` 範例或 `/var/lib/findb-account/`）；
若已存在 ledger，所有本機命令應明確沿用其絕對路徑。需要搬遷時，先備份並以 SQLite
一致性備份完整移轉至使用者可寫的固定位置，再讓全部 consumers 同時切換；保留 pins、
debt、cooldown 與相關 allocation，不能以空白新檔取代已知 ledger。首次且確定無既有
ledger 時才使用範本的新路徑。本機 `.env` 僅供 local，staging／production 繼續使用各自
部署設定與 durable mounts；未明確設定路徑的 deployed fallback 維持 `/var/lib/findb-account/governor.sqlite3`。

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

保留的 legacy `configs/twelve_data_us_common_stocks.v1.json` 包含 AAPL、MSFT、NVDA；staging 使用上方新版兩商品 pilot。修改 symbols 或
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

四個 staging scheduler container 使用 `--run-forever`，分別服務 Twelve Data、FinLab、Shioaji、TAIFEX；
stopped 時保持 idle，不建立 provider cycle。state directory 應由 staging deployment 以
UID/GID `10001:10001`、mode `0700` 的 durable volume 掛載。

常駐CLI會將container runtime的`SIGTERM`及互動式`SIGINT`轉成shared stop event。Idle loop
會立即以exit `0`結束；若signal落在final running preflight後，runtime不會跨入新的provider
cycle；已開始的cycle仍會完成terminal report再結束。Deployment要求stable container在30秒
grace period內exit `0`，exit `137`一律視為no-go並恢復原stable。
TAIFEX pilot 在 signal 後保留 prepared state、取消 terminal polling，不推進 checkpoint；restart 使用同一 identity 重試。

## Provider raw storage（Cloudflare R2）

Twelve Data 保存 exact HTTP bytes；FinLab 保存 deterministic SDK bundle；Shioaji 保存
SDK-detached acquisition snapshot；TAIFEX 保存兩份官方 report 及 checksum manifest。四者都在 Source delivery 前完成 raw-first persistence，
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
| `FETCHER_ACCOUNT_STATE_PATH` | 本機新抓取是 | 全 providers／Pilot／Full／historical 共用 durable account governor；本機範本從 `fetcher/` 使用 `.state/account/governor.sqlite3` |
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

## 全市場手動排程

`findb-fetch-full-market` 支援 local/staging/production 對應的 `configs/full_market.<target>.v1.json`，
一律初始 stopped，`FULL_MARKET_ENABLED=false` 預設。Owner published official baseline 與完整
published calendar 通過後，手動啟動既有 scheduler，凍結 ready scope 與首次日期。
Cycle 官方名單刷新成功或失敗後仍使用原 admitted scope；standalone maintenance refresh
依 Source 回傳的 `authorized_dataset_keys` 執行，可在尚無 baseline／admission 時 bootstrap，
但不可省略有效且非空的 provider／dataset scope 或 credential 期限驗證。
所有環境的新抓取均移除 readiness／enrollment／expiry／capacity 前提。Source／provider API
認證、running control、flag stop 與資料驗證保留。詳見 [Full market runbook](../docs/operations/full_market.md)。

Pilot／Full checkpoint 分離。Current staging／production 部署的 bounded／Full／historical
consumers 一律掛載環境內共享且可寫的 account state，包含 `FULL_MARKET_ENABLED=false`
的首次 bounded 安裝；保留既有 ledger／credential pins，不要求 readiness 證據。
已設定 account state 的 consumers 共用 durable governor，沿用既有
account ID 或使用 credential fingerprint；不設跨環境分配、所有行情的日請求預算、deadline capacity proof
或 enrolled response bounds。Shioaji login 另採本地保守 rolling 86400 秒最多 1000 次，非 UTC／provider reset epoch；
僅計 actual login start／unknown login liability，SDK error 亦計，Kbars／其他 bootstrap／cleanup 不占 login 次數。只套用 Twelve Data 1597 credits／分鐘、FinLab API 每日 5000 MB、
Shioaji 每日 500 MiB 與 50 行情查詢／10 秒；TAIFEX 為公開 OpenAPI，以 429 cooldown 處理。
首次升級以可用的舊 allocation account ID 建立持久化對照；完成 pin 後，證據檔變動不重設
ledger。Twelve Data fingerprint 跟隨 provider config 去除 credential 前後空白；FinLab 1.5.7
的 gateway、identity 與 allocation 比對一致使用 `credential.strip()[:64]`，符合 native
`LoginPanel.login`／`get_token` 的前 64 字元選取。非空白長 token 的後綴不代表另一帳號，
同一有效 credential 的格式變動沿用已 pin 的用量、配額與 cooldown；Full／Pilot／historical Shioaji
也一致去除 key／secret 前後空白。所有 SDK 入口依實際傳入的 key 綁定 account，顯式 key 不受其他
environment key 覆蓋；FinLab 保留明確提供與 SDK 等值 installed token 的 raw、舊去空白、
新 SDK 等值 fingerprints，proven pins 以 literal allocation prefix 冪等合併已知 debt／cooldown。若首次 pin 前 allocation 遺失、損壞或缺少可用 account ID，無法辨識舊 ledger，
則使用 credential fingerprint 與平台實際配額，保留原匿名用量／cooldown 供稽核。
此例外接受舊用量與 cooldown 無法接續的風險，不猜測合併舊債務；升級前保留 allocation
與 governor 可維持接續。並行 pin／merge 使用 identity mutex、排序 account locks 與短 SQLite transaction。
FinLab 用量以平台 SDK API 為權威，detached bytes 不代表 network usage；Shioaji 在抓取前查
實際 SDK 累計 bytes。Source API 自行執行 DB-backed key rate limit 與 429。
Twelve Data HTTP transport boundary 同樣使用 client 的實際 config key，環境 key 不得改寫帳號
lock／debt／cooldown。FinLab 1.5.7 的 `login` 只修改 process-global token，所有 gateways
以 process-wide SDK lock 保護 token 選取至 `data.get` 完成與 durable finalization；SDK lock
先於 account lock，拒絕授權釋放 lock，同 thread recursive dispatch 明確拒絕。
Account identity 在 login 前建立，結束 scope 還原 SDK 修改的 token environment，避免污染
installed allocation 身份；`from_env` 與 central identity 使用同一 SDK scope 的 installed
environment 快照。其他 thread 等待還原完成，同 thread authorization／finalization callback
讀取 scope 前的 installed token，避免重入鎖造成 deadlock；此還原不重設 governor 用量或 cooldown。
已 pin 的 identity callback 僅在全部已知 fingerprints 都指向同一 target 時唯讀回傳；
持有 account lock 時若仍需建立／合併 pins，明確拒絕，離開 operation 後才可執行 merge。
外部 account lock 內不允許巢狀 SDK dispatch；snapshot 不等待 SDK lock，busy 時拒絕以維持鎖順序。
Governor rollback inspection 也計入跨 UTC 換日仍有效的 unknown rolling login liability。

Shioaji child 啟動為 dormant；SDK construction、login、lazy TSE／OTC contracts 與 Kbars 都先取得
同帳號 lock、durable armed intent、child FD 與 current scoped Source GO，才開始該 SDK operation。
Source header receipt 起最多 50ms，且受 actual minute／historical lease expiry 限制；不得由 dispatch
時間更新 origin。Source 取消／停止／撤銷後不登入。首次 login 前 authenticated usage 不可用，只記錄
`not_authenticated`；login 後 current SDK bytes／connections 必須有效，才可查 contracts／行情。
已知 fresh connections=5 不重新登入；有效既有 session 可沿用。未知其他 key 的 person_id 不推定關聯，
首次連線由平台拒絕／接受，native acceptance 另留 official connections 與 login/cleanup evidence。
Parent SIGKILL 後 child 持有同一 lock FD 至 durable completion／cleanup；logout 只關閉既有資源，
不登入或抓新資料，不要求 Source running，採 zero-unit cleanup intent 與同帳號 lock。已知 SDK login
失敗、Kbars 實際成功／錯誤與 cleanup 結果在首次 durable finalization 保留 audit；
Kbars 錯誤仍計一次 actual request，cleanup 為 zero-unit，原 request debt／cooldown 不退款。
SDK child argv／env／GO 沒有
Source、cloud 或其他 provider secrets，只有 private credential pipe 傳 provider keys。

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

正常抓取不讀 readiness／account allocation 的配額、expiry 或 proof；`--readiness-file` 與
舊 enrollment/repair 工具保留相容診斷用途。治理資料只要求 published baseline 與完整 calendar。
已有 identity 對照的 account usage、pacing、429 cooldown、prepared payload 與 checkpoint
不因重啟或升級重設；首次 pin 無法辨識舊 ledger 的例外與風險見上文。
舊 capacity violation 不再封鎖新抓取；HTTP identity encoding、Content-Length、串流大小、
SDK／IPC／R2 bounds、schema 與 provenance 驗證保留。不合法 response 不建立成功 raw 或 prepared body。
FinLab 1.5.7 對 dataset/date 取得五個 OHLCV 欄位，再投影成各商品 bundle，成功 cache 供商品共用。
Shioaji 1.7.1 由靜默常駐 child process 執行 data API，SDK usage 無法確認時拒絕交付。

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

### Staging pilot 啟動與驗收

部署先準備 `findb/staging/fetcher/api/source/taifex` 專用 secret；TAIFEX 不需 provider token。
FinDB 以 `reconcile_fetcher_credentials.py --deployment-target staging --runtime-profile bounded`
接收四把 Source keys 與 calendar Serve key 的五個 SHA-256 hashes；不可傳 plaintext 或開跨 unit secret 權限。
`provision_registry.py --deployment-target staging` 啟用 `tw_futures_eod` bounded incremental policy
（18:00、minimum rows=4），新增 stopped 的 `taifex_tw_futures_pilot_v1`，保持 full-market controls 停止。
`seed_staging_pilot_calendar.py --deployment-target staging` 僅發布缺少的官方 TAIFEX 2026 日曆，
保留 operator revision，不改 US／TW／HK。calendar 缺少或 credential 不可用即 fail closed。

Owner 在 Dashboard 啟動以下 controls；container startup/readiness 允許 desired=stopped，部署不代為 start。

| Provider | Control key | CLI（附 `--check` 或 `--run-forever`） |
| --- | --- | --- |
| Twelve Data | `twelve_data_us_common_stocks_daily_v1` | `findb-fetch-scheduler --schedule-file /app/configs/daily_scheduler.staging.v3.json --slot-id western_markets_window --dataset-key us_equity_eod` |
| FinLab | `finlab_tw_equity_eod_v1` | `findb-fetch-finlab-scheduler --schedule-file /app/configs/daily_scheduler.staging.v3.json` |
| Shioaji | `shioaji_tw_pilot_v1` | `findb-fetch-shioaji-scheduler --manifest /app/configs/shioaji_tw_staging_pilot.v3.json` |
| TAIFEX | `taifex_tw_futures_pilot_v1` | `findb-fetch-taifex-pilot --config /app/configs/taifex_tw_staging_pilot.v1.json` |

Twelve state 改用 `/var/lib/findb-fetcher/staging-pilot-v2/state.sqlite3`；Shioaji 改用
`/var/lib/findb-shioaji-fetcher/staging-pilot-v3/state.sqlite3`。舊 immutable manifest／state 保留作 rollback；
不移入舊 pending NVDA／ETF prepared requests。TAIFEX 使用 `/var/lib/findb-taifex-fetcher/staging-pilot-v1/state.sqlite3`。
只選 source-observed `YYYYMM` 近月，每個產品保留兩個 session；weekly、價差與 continuous alias 不進 pilot。
兩份原始報表先寫 R2，raw manifest 綁定各自 checksum，再凍結 contract/prepared request；restart 重送同一 identity。
耗盡 request／attempt budget 顯示 blocked；acquisition/Source 錯誤不算 no_data。

新增 provider 必須擴充 validated catalog、supported runtime/schema、最小 Source scope、offline startup 與
stopped→running fixture、實際 acquisition/durable replay 測試，並收集每個 feed 最新 eligible completed trading day
五個 feed 的 Source 202、raw、terminal normalization、exact pilot canonical 與 Serve proof。
`infra/acceptance/collect_staging_feed_evidence.py` 的 `pilot_functional_complete` 是此門檻；
multi-date metrics 是額外資訊。Fixture 只證明實作行為，不證明 live credentials、供應商授權或資料流已通過。

Staging functional target 由 backend unit-local published 官方日曆的完整 revision 與 UTC 觀測時刻
推導；US 依台北 08:15 trigger／lag 1，TW EOD／minute 14:30，TAIFEX 18:00，並檢查官方收盤。
US 09:00 是交付 deadline；minute 過 17:00 cutoff 仍須有同 target 的完成 proof。
日曆缺漏、revision 不符及最新日期 missing／blocked 不可回退採用舊成功。
Exporter 每 run 保留 accepted／duplicate 的所有 Source receipts，但 canonical coverage 只計一次，
使 durable prepared retry／restart 的同一 run 重播仍可驗收。outbox 歷史 delivery generations 聚合保存，
目前 generation 依 normalization job 的 delivery_id 核對，正常 lost delivery／expired lease recovery 不重複
canonical proof。Serve 回應須以 source、source_fetched_at、OHLCV、minute 精確 bar 或 futures actual contract／session
與 run-linked canonical sample 正規化比對，collector 重算 SHA-256 fingerprint 後才接受 run binding；
同 key 的其他來源或內容不算成功。Fixture recovery／HTTP 測試仍不代表 live credentials 驗收。
Fetcher 不讀 Backend DB 或 import Backend。

Live evidence collector 在同 unit 的可信 wrapper 內壓縮 standalone probe JSON，stdout inline
或拆成最多 16,000 字元的 base64 chunks，避免 SSM 24k stdout 截斷。大型資料暫存在 container
`/tmp` 的唯一 nonce artifact（0600），每次收集有 8 MiB raw／2 MiB compressed／175 chunks 與
180 秒上限，依序驗證完整 SHA-256 與 bounded 解壓後才接受原始 JSON。所有結果都 best-effort
清理暫存檔；下次 probe 只移除超過 24 小時、同 owner 的專用 prefix 孤兒檔。
Probe 的 standalone stdout JSON、UTC 真時鐘與 functional acceptance 語意維持原樣；
傳輸不新增 S3、IAM、SDK 或跨 unit credentials。完整測試與清理規則見
[`docs/operations/testing.md`](../docs/operations/testing.md)。
