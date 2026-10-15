# FinDB Fetcher

Fetcher 是獨立 application，不 import Backend、不連 FinDB DB；透過受保護 Source API 交付 versioned contracts。
環境身份由 `APP_ENVIRONMENT=local|staging|production` 載入，須符合 manifest 的 deployment_target。

## 共用七條 feeds

| Provider | Datasets |
| --- | --- |
| Twelve Data | `us_equity_eod`、`hk_equity_eod` |
| FinLab | `tw_equity_eod`、`tw_etf_eod` |
| Shioaji | `tw_equity_minute`、`tw_etf_minute` |
| TAIFEX | `tw_futures_eod` |

唯一 recurring runtime 是 `findb-fetch-runtime --execution-profile pilot|full_market`。
兩種 profile 使用同一 published universe、calendar、provider adapters、Raw R2、Source 與 retry/catchup 流程。
Pilot 建立 daily plan 時按 canonical 商品識別排序取前兩項，期貨以 product／contract 取樣並保留所有 sessions。
樣本建立後凍結；重試、重啟、補抓及 universe 發布不得改寫它。

兩環境固定安裝八個 recurring consumers，以及三個獨立 historical consumers。
`FULL_MARKET_ENABLED=false` 只禁止 full market 新抓取，不影響安裝、pilot 或 prepared delivery。
部署只接受 stopped controls；Owner 操作啟停。Staging 常態開 pilot，上版可短暫開 full market；production 常態只開 full market。
`pilot_<provider>_v1` 與 `full_market_<provider>_v1` 各自有 first dates、plans、checkpoint；同環境共用帳號 governor。
舊 provider-specific pilot CLI 僅為既有診斷工具，已退出正式部署。

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
| `--state-path` | 部署是 | 各 provider／execution profile 的獨立 durable SQLite checkpoint 檔案 |
| `FETCHER_ACCOUNT_STATE_PATH` | 本機新抓取是 | 全 providers／Pilot／Full／historical 共用 durable account governor；本機範本從 `fetcher/` 使用 `.state/account/governor.sqlite3` |
| `FINLAB_API_TOKEN` | FinLab 是 | FinLab headless SDK token |
| `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` | Shioaji 是 | 該環境的 Shioaji credentials |
| `SHIOAJI_SIMULATION` | Shioaji 是 | 預設 `true`；明確 `false` fail closed |
| `TWELVE_DATA_API_KEY` | Twelve Data 是 | Twelve Data runtime secret |
| `CLOUDFLARE_R2_ACCOUNT_ID` | delivery/scheduler 是 | Cloudflare account ID |
| `CLOUDFLARE_R2_RAW_BUCKET` | delivery/scheduler 是 | Fetcher-owned private raw bucket |
| `CLOUDFLARE_R2_RAW_ACCESS_KEY_ID` | delivery/scheduler 是 | Raw bucket R2 access key |
| `CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY` | delivery/scheduler 是 | Raw bucket R2 secret |

每個 provider 使用自己的 Source key、credentials、container 與 state。Secrets 只從
該環境的 AWS Secrets Manager，由 instance role 載入，不進 log、contract、raw payload 或
Dashboard client bundle。

## 排程與持久化

`configs/full_market.<target>.v1.json` 由 Backend 共用 feed catalog 產生，環境間只有 target 不同。
首次啟動需 published baseline 與完整 published calendar；不要求 capacity readiness 證據。
獨立 checkpoint 綁定 execution profile，拒絕把既有 full market SQLite 改標為 pilot。
Daily plan API 未指定 profile 的舊 caller 仍表示 full market；本 runtime 一律明確帶入。
canonical 更新的持久化寫入證據按 plan 保留，另一 profile 的寫入不會讓已完成計畫倒退。

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

## 部署與驗收

AWS 設定、首次切換備份／drain、immutable acceptance 與 rollback 下限見
[部署文件](../docs/operations/deployment.md)；Owner 操作與缺漏判定見
[Full market runbook](../docs/operations/full_market.md)。
兩環境使用相同部署健康 gate，native provider 抓取不是日常部署 gate。
七條 feeds 的 fixtures 驗證與真實 provider／R2 驗收分開記錄，尚未解決的 native 問題保留 backlog。
