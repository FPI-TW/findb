# Full market 啟動、平台配額與治理資料

`FULL_MARKET_ENABLED=false` 是 local、staging、production 的預設值。Backend 與 Fetcher 必須使用
相同 `APP_ENVIRONMENT=local|staging|production` 與旗標。既有 JSON `deployment_target` 仍是環境匹配契約。
旗標值來自 committed control plane `infra/env/<target>/<unit>.json`，是 runtime kill switch，不再選擇
deployment profile；每個 target 的部署一律安裝四個 Full workers。
旗標為 true 不會啟動排程。Full market 的啟動資料條件只包含已發布 baseline 與完整已發布 calendar。
Owner 權限、revision CAS 與 Source 認證屬於既有操作／資料存取規則。
TAIFEX 官方 OpenAPI 不需 provider 帳號、API key 或 provider secret。部署設定中的
`FETCHER_TAIFEX_SOURCE_CLIENT_KEY` 是 **FinDB 自己簽發的 Source API key**，供 TAIFEX Fetcher
存取 control／authorization／universe／plan、回報 heartbeat 並交付資料；它不是期交所的 API key。
只有下載官方報表時無須此 key，走正常 FinDB Source API 時仍須認證。其 Secrets Manager 路徑為
`findb/{staging|production}/fetcher/api/source/taifex`，不宣告 `provider/taifex` secret。
Full API 的 Source credential 必須有明確、非空的 dataset list，且涵蓋 admitted feeds；
NULL、空值或 malformed scope 一律拒絕授權與 universe／plan 存取。
maintenance bootstrap 也必須通過有效 provider／dataset scope 與 credential 期限檢查，
但無須先建立 admission 或發布 baseline；授權回應的 blockers 僅反映 maintenance 條件，
並以 `authorized_dataset_keys` 限制官方名單刷新。Running cycle 刷新成功或失敗後，
universe、plan 與新資料抓取仍僅使用當次凍結的 admitted feeds。正常 credential create／rotate
會將省略的 scope 展開成 provider 的明確清單；本規則不放寬既有 API 邊界。

## Staging baseline 與 calendar 現況（2026-10-12）

17:02（Asia/Taipei）以唯讀 DB 查核，七個 feeds 的首次 official baseline 均已 `published`。
每個 release 的實際成員數與 Python canonical ordering SHA256 均與提交值吻合；官方名單、
provider catalogue 與 checksum 證據已保存至 staging R2 `findb-staging-raw`。所有成員完整保留，
未縮小名單掩蓋 classification／mapping gaps；gap 成員繼續依既有 blocked 規則處理。

| Provider / dataset | 成員 | mapped | gap | Effective date | Published release ID |
| --- | ---: | ---: | ---: | --- | --- |
| `twelve_data/us_equity_eod` | 6023 | 5081 | 942 | 2026-10-12 | `01a12f4f-310b-78e2-ac44-3ff6cf3bed60` |
| `twelve_data/hk_equity_eod` | 2826 | 2778 | 48 | 2026-10-12 | `01a12f4f-396e-795d-bac0-c0a3c81debc1` |
| `finlab/tw_equity_eod` | 1947 | 1947 | 0 | 2026-10-12 | `01a12f48-721b-7902-a6d5-d9cef496ed4a` |
| `finlab/tw_etf_eod` | 360 | 359 | 1 | 2026-10-12 | `01a12f48-793e-7993-8bc6-d14c85cfe578` |
| `shioaji/tw_equity_minute` | 1947 | 1945 | 2 | 2026-10-12 | `01a12f48-899c-726e-824a-fe89a2da126e` |
| `shioaji/tw_etf_minute` | 360 | 358 | 2 | 2026-10-12 | `01a12f48-90e5-7b8c-980b-7022074ef795` |
| `taifex/tw_futures_eod` | 62 | 62 | 0 | 2026-10-08 | `01a12f48-7c7b-7d37-b07b-393421c885c4` |

TAIFEX 官方最新報表日期為 2026-10-08，含 30 個月契約與 1 個週契約，展開實際 regular／after_hours
共 62 個成員。US 的 942 個 gap 包含 424 個 classification gap；HK 的 48 個 gap 包含 30 個
classification gap。FinLab mapping 使用認證 SDK `price:收盤價` 欄位清單；Shioaji 使用既有
simulation SDK contract catalogue。此 baseline 不宣稱 gap 商品可抓取，也不代表行情已完成導入。

| Market | 2026 published calendar revision | 完整性 |
| --- | --- | --- |
| US | `019fc6e3-4d25-78ff-94d2-70975d5e6b9d` | 365 / 365，2026-01-01 至 2026-12-31 |
| HK | `019fc6e8-cf14-7f68-aaf8-b9bb76bd788f` | 365 / 365，2026-01-01 至 2026-12-31 |
| TW | `019fb28f-2561-72b8-a96c-1d45f00da3e5` | 365 / 365，2026-01-01 至 2026-12-31 |
| TAIFEX | `01a10ad2-8804-70e3-b48d-bb108225369a` | 365 / 365，2026-01-01 至 2026-12-31 |

Source submission 使用四個既有 scoped credentials，沒有變更 Source 權限。Twelve Data 的完整
US／HK 請求以 compact JSON 與頂層 immutable evidence 提交（931338／502379 bytes），符合
現行 nginx Source 1 MiB 上限；未重複每個 member 的 optional raw reference，所有原始檔仍有
R2 object URL 與 SHA256。TW ISIN 原始快照超過 8 MiB，此次一次性保存上限為 16 MiB，
沒有修改部署中的 raw storage 設定。

發布 checksum 必須使用與 submission 相同的 Python `member_key` 排序，不能依 DB collation：
US 含標點／大小寫 symbol，TAIFEX session 名稱會產生不同順序。此次在單次 management process
載入已測試的排序修正，再經原有受保護 Admin ASGI HTTP route 的 Owner 認證、checksum 驗證、
transaction 與 audit 發布；未修改 installed files 或重啟服務。Owner 機器憑證由使用者明確授權、
有效一小時，明文只在程序記憶體；`01a12f57-abdf-78c1-a2c4-df2d9ed27977` 已於
17:00:37 撤銷。七筆 `publish` 與 credential create／revoke audit 已持久化。

Nasdaq catalogue 的 `XNCM`／`XNGS`／`XNMS` 依 [ISO 10383 MIC](https://www.iso20022.org/market-identifier-codes)
對應 operating MIC `XNAS`，只用於 identity matching；仍核對 symbol、currency、asset type 並拒絕
duplicate mappings。MIC 與穩定排序修正已於 PR #290 合入 protected `main`
`c744a3672c8d794b1725c5a59e266f2752247865`；[FinDB staging CD](https://github.com/FPI-TW/findb/actions/runs/38229909196)
與 [Fetcher staging CD](https://github.com/FPI-TW/findb/actions/runs/38229909124) 均成功。
Production rollout 紀錄見 [deployment runbook](deployment.md#production-v016-rollout2026-10-13)；
staging baseline publication 紀錄不代表 production 已有相同資料。

最後查核 DB revision 為 `3553ed95cfbf`。四個 Full controls 均 desired／observed `stopped`、revision 5；
本次沒有變更任何 scheduler。Pilot controls 的 desired state 已由其他操作設為 running，
不能沿用先前「八個 controls 均 stopped」的查核結果。Baseline 與 calendar 的啟動資料已備齊，
Owner 手動 start 仍是獨立操作。

SSM 證據：Twelve Data candidate `5e8433d3-c112-4071-b952-d2ba73ef9a58`、FinLab candidate
`3238db7b-d8bc-4b2a-a721-c8c3e59e5e39`、Shioaji candidate `d7a76023-37c0-42e8-9cb5-a306d79de97e`、
TAIFEX candidate `2bb03a64-515b-422a-a01d-e12f603c6273`、發布
`8549a650-c0ed-4160-bb16-15aeb80d0402`、最後唯讀校驗
`a2ae53d0-e4ce-4927-93dc-692504ceff09`、四個 provider 既有 Source credential 的公開 API
回讀 `eaf5ba97-b196-434f-aef8-ed726bb63390` 均為 Success／exit 0。
本機驗證：全 repo `pnpm check`、Backend Full market／time gate suites（137 passed）、
Fetcher Full market／recovery／governor suites（334 passed）。

## Production baseline 與 calendar 現況（2026-10-13）

11:02（Asia/Taipei）七個 feeds 的首次 official baseline 均已透過正常受保護 Source／Admin
API 提交與 Owner 發布。官方快照與 production 帳號 catalogue 重新取得，原始證據、URL 與
SHA256 保存至 production R2 `findb-production-raw`；沒有複製 staging release。
後續獨立唯讀後驗逐一比對全部成員與提交 payload、member count、Python canonical ordering
SHA256、effective date、evidence 及 publication audit，七個 feeds 皆 `ready=true`、blockers 空。
這只確認 baseline／calendar 啟動資料齊備，不代表 runtime opt-in 或行情資料驗收完成。

| Provider / dataset | 成員 | mapped | gap | Effective date | Published release ID |
| --- | ---: | ---: | ---: | --- | --- |
| `twelve_data/us_equity_eod` | 6024 | 5082 | 942 | 2026-10-12 | `01a13336-4cdb-7117-a66f-c7771e57adf2` |
| `twelve_data/hk_equity_eod` | 2821 | 2774 | 47 | 2026-10-13 | `01a13336-51c9-7c6f-bca1-114e68e8204f` |
| `finlab/tw_equity_eod` | 1947 | 1947 | 0 | 2026-10-13 | `01a13336-5431-701c-8e3c-dcb64b757f45` |
| `finlab/tw_etf_eod` | 361 | 359 | 2 | 2026-10-13 | `01a13336-559d-71f0-bdc6-13acaefd8795` |
| `shioaji/tw_equity_minute` | 1947 | 1945 | 2 | 2026-10-13 | `01a13336-564a-731d-8fec-1b6bde2d42bf` |
| `shioaji/tw_etf_minute` | 361 | 359 | 2 | 2026-10-13 | `01a13336-5849-73a4-b18a-1958a7bb2507` |
| `taifex/tw_futures_eod` | 62 | 62 | 0 | 2026-10-12 | `01a13336-58d9-7e19-bf84-ecb34ae33fe1` |

TAIFEX 共 31 個實際月／週契約，regular／after_hours 展開為 62 個成員。
所有 classification／mapping gaps 保留；gap 商品仍依既有 blocked 規則處理，沒有縮小官方名單。
TW ISIN 原始快照超過 8 MiB，單次 R2 保存用 16 MiB 上限，沒有修改部署中的 raw storage 設定。
US／HK 使用 compact JSON 與頂層 immutable evidence，分別為 931,508／501,508 bytes，
符合 nginx Source 1 MiB 上限。Shioaji production 為 simulation、SDK 1.7.1；隔離 runtime
catalogue 呼叫曾失敗，另以無輸出的隔離 child 成功取得原始 catalogue，再套用同一個
exact exchange／currency mapping 與 duplicate rejection。此準備不代表 Full runtime 已通過驗收。

| Market | 2026 published calendar revision | 完整性 |
| --- | --- | --- |
| US | `01a13336-596e-7025-be83-129ed0ad49bd`（revision 2） | 365 / 365，2026-01-01 至 2026-12-31 |
| HK | `01a10eb8-521b-789b-a799-e86154323b67`（revision 1） | 365 / 365，2026-01-01 至 2026-12-31 |
| TW | `01a0c213-665e-7e20-87f3-ed45ea0f976d`（revision 1） | 365 / 365，2026-01-01 至 2026-12-31 |
| TAIFEX | `01a10eb8-52ba-772f-978d-ae31a6ce5544`（revision 1） | 365 / 365，2026-01-01 至 2026-12-31 |

US 舊版已以正常 Admin JSON preview／apply／publish 與 revision CAS 更新至
`us_equity_2026.v2.json`。全年所有日期、開休市、holiday／description 與每日 session 均與
reviewed source 逐項一致；11-27、12-24 於 America/New_York 13:00 提前收盤，
其餘 open sessions 為 09:30–16:00。已再次核對
[NYSE 官方日曆](https://www.nyse.com/trade/hours-calendars)。
Reviewed source file SHA256 為 `655c9b6e9c974ca0f6596f5f00fcfb8ac6dc3fc6dde11ed10a3863e02afb922b`，
保存於 bootstrap credential create audit；此次 Owner JSON import 的 US revision 2 為
`source_kind=json`、`source_sha256=NULL`，revision 沒有另外保存正規化 rows 摘要。
完整 365 rows 的逐項比對與原始 reviewed file 摘要是分開的驗證證據。TW、HK、TAIFEX 沿用既有正確 revision。

使用者明確授權有效一小時的 Owner machine key 及四個 dataset-scoped Source bootstrap keys。
所有明文只留在 production management process 記憶體；每個 provider 的 Source key 使用後即撤銷，
Owner key `01a13336-4bee-776c-8568-d73096ac25e2` 於 11:02:42 撤銷，五個 keys 均完成
create／revoke audit。正常 Admin 無認證查核回傳 401；七筆 baseline publish 與 US calendar
preview／apply／publish audit 均持久化，沒有依賴 auth override 或直接寫入 baseline／calendar tables。
使用者授權暫存的本機完整 payload 及可重組傳輸片段已刪除；只保留 checksums、summary、
command IDs、scripts 與非機密操作結果。

實際 production HTTPS credential 驗證：Twelve Data US 200／HK 403，FinLab equity 200／ETF 403，
Shioaji equity／ETF 皆 200；calendar Serve key 對四個市場皆 200／coverage complete。
三個 provider API credentials 已完成實際 catalogue 操作。TAIFEX 沒有 active production Source
client，也無 `findb/production/fetcher/api/source/taifex` secret；本次短期 TAIFEX key 已撤銷。
HK、FinLab ETF scopes 與 TAIFEX 長期 credential 仍須正式配置，bootstrap 不改動既有 credentials。

Backend／Fetcher Full flag 維持 false，仍為 bounded profile。四個 Full controls 保持 stopped／revision 1，
首次日期與 admission 尚未建立；三個 Pilot controls 在本次操作前已由其他操作設為 stopped／revision 9，
本次未變更任何 control。發布前後唯讀比對確認長期 Source scopes／expiry／revocation
與全部 control revisions 相同；不可沿用 v0.1.6 部署完成時 Pilot running 的舊查核。

SSM 證據：初始 inventory `f54f7f9c-9815-43ce-b4e2-6181021b1878`、reviewed calendar checksum
`0fa0efb1-8d94-4a53-8c9f-c935239757f7`、初始 HTTPS credentials
`a9f35ee4-1f99-4ed9-957e-d432ea92c284`；準備 Twelve Data
`39f09bc9-b780-453b-9b95-7b7c54bae912`、FinLab `e3c00bc6-7360-4458-a919-de248f8f9e4f`、
Shioaji `0eddf2cc-ab60-4a87-853c-2ed60d26ace7`、TAIFEX `0bdf8478-3c6d-46d6-ab43-761b043670c0`；
正式發布 `785150c8-a14f-4880-bc16-4d7c5331bd84`、獨立唯讀後驗
`c7e84b30-4bd5-44e1-84a7-9a7c62707e32`、公開 HTTPS 後驗
`41f297da-50bf-4c94-9520-32da45bfd649` 均 Success／exit 0，US calendar revision 2 已可讀。
本機非機密證據保存於 `/private/tmp/findb-production-baselines-20261013`。

## Production Full scheduler 尚未回報查核（2026-10-13）

11:13（Asia/Taipei）唯讀查核顯示，Owner 於 11:08:43–45 將四個 Full controls 從 revision 1／stopped
設為 revision 2／running，admission 已凍結七個 ready feeds；11:10:02–04 又以 user actor 設為
revision 3／stopped。四者 heartbeat／cycle timestamps 均為 null，沒有 runtime error report。
Dashboard start 僅更新 desired state，不會部署或建立容器；沒有 heartbeat 時顯示尚未回報，
不能把已接收 Owner start 當作 worker 已開始運行。

實際 production Fetcher 仍為 `fetcher-v0.1.6` bounded profile（manifest 省略 runtime_profile
代表 bounded），只有三個 Pilot scheduler 與三個 historical workers，沒有任何
`findb-full-market-*` 容器。Backend 與 Fetcher 有效 `FULL_MARKET_ENABLED` 都是 false。
這是未回報的直接原因，與七個已發布 baseline／calendar 是否齊備無關。
正式運行仍須先修正已記錄的 pacing commit-delay race，完成 Full profile 正式部署、同步兩端
flag，然後由 Owner start；長期 Source scopes／credentials 已完成後續配置，見下方更新。
不新增 readiness／enrollment／capacity gates。

本次 start 已保存 immutable first dates：US 為 2026-10-12，其他六個 feeds 為 2026-10-13。
停止、後續部署與重啟均不重設日期；後續只補首次日期之後的 gaps。本次診斷只讀取狀態，
沒有變更 flags、controls、credentials 或部署。

Production SSM：Backend `beeddeca-30f7-460a-906b-49904a523e16`、Fetcher
`57429b11-5816-45f9-9a6c-9233c5b35273`。Staging 即時對照：有效 Full flag true，
`findb-full-market-taifex` 為 running，active `fetcher-taifex` Source client
`01a10ada-89c5-7148-a02d-a8499e93f9c5` 限定 `tw_futures_eod`；
`findb/staging/fetcher/api/source/taifex` 有 AWSCURRENT 版本
`b6cda8e9-cc9e-4d14-9ebe-1e8d1d87b68c`。容器有 FinDB Source key、沒有 TAIFEX provider key，
與兩層認證的設計一致；實際 authenticated Source HTTPS 回應 200，未輸出 key 明文。
Staging SSM：Source client `14426af7-1330-41b8-9712-97121a79f507`、容器與 HTTPS
`593021f1-3bfd-4cac-823f-d28956247e88`，皆 Success／exit 0。
唯讀證據保存於 `/private/tmp/findb-full-market-diagnosis-20261013`。

## 啟動契約

Full market 每個 feed 僅檢查兩項啟動條件：

1. 同 provider／dataset 有 `published` official baseline，且 effective date 不晚於交易所當地今天。
2. 對應交易市場當年度有完整 `published` calendar revision。

至少一個 feed 同時具備這兩項資料即可啟動，並只凍結符合條件的 feeds。
Enrollment、installation receipt、readiness ACK／expiry、account／capacity proof、環境旗標與
registry 額外 metadata 不列為啟動資格 gate。舊 `full_market` metadata 若為字串或 list，
start 視為缺少 optional governance，重新建立 activation date；有效 contract declaration 與
既有 immutable first date 仍保留。未知 scheduler／scope 仍依 API definition validation
處理，Owner 操作權限與 CAS 保留。

Owner 透過 `PATCH /api/v1/admin/schedulers/{scheduler_key}`，傳入 `desired_state=running`
與 `expected_revision`。Backend 在同一 transaction 重查 baseline／calendar，凍結 ready datasets、
baseline IDs、calendar revision IDs 與首次日期，並寫入操作 audit；admission 不必綁定 enrollment。
新的 ready sibling 須停止後再啟動，重複 running PATCH 不改凍結範圍。
每 dataset 首次日期由交易所當地今天決定；migration 保留合法 legacy activation_date，malformed
legacy 日期在首次 start 時以 immutable first date 修正。停止、重啟與重新 enrollment 均不重設日期。舊 `/admin/feeds/{dataset}/activate`
與 `/deactivate` 在 Owner 認證後回傳 410；沒有 per-feed activation、manual readiness_approved 或五日門檻。

每次 provider call 前立即查 effective Source authorization。旗標關閉、Owner 停止、Source credential 撤銷／到期或 calendar 不完整時，禁止新抓取與 plan。已保存 prepared payload／frozen plan 的 delivery、
normalization、canonical 與稽核可繼續；不重抓已 prepared 資料。關閉旗標由 ingest/all lifespan 或
下述管理命令停止 Full controls 並寫 audit；GET 投影永遠唯讀。重新開旗標後仍需 Owner 手動啟動。

## 平台限制與用量

平台配額只採用以下已確認限制；未公布的維度不新增數值配額、預算或啟動門檻。

| Provider | 適用限制 | 用量核對與超限處理 |
| --- | --- | --- |
| Twelve Data | 1597 API credits／分鐘 | 以 `/api_usage` 與 response credit headers 核對實際 credits；依 endpoint weight 計算，收到 429 時等待／重試 |
| FinLab | 5000 MB／日 | 以 SDK API 回傳的 `quota`／`limit_size` 核對帳號累計用量，達限後等待平台重置 |
| Shioaji | 500 MB／日、行情查詢 50 次／10 秒 | 以 `api.usage()` 的 bytes／limit_bytes／remaining_bytes 核對；API 回報的 524288000 bytes 為 500 MiB；依 10 秒視窗控制查詢速率 |
| TAIFEX | OpenAPI 不需帳號，未公布明確 requests／bytes 數值上限 | 以 HTTP 429 作為觸及平台上限的訊號，依 `Retry-After` 或既有 backoff 等待／重試，不自行設定 provider 配額數值 |

Twelve Data credits 計算見[官方說明](https://support.twelvedata.com/en/articles/5615854-credits)；
Shioaji 限制見[官方使用限制](https://sinotrade.github.io/zh/tutor/limit/)；TAIFEX 平台與 endpoint
定義見[官方 OpenAPI](https://openapi.taifex.com.tw/)及[Swagger schema](https://openapi.taifex.com.tw/swagger.json)。
TAIFEX 429 的等待策略採用現行 runtime 的 rate-limit handling。

安裝 Full profile 時，保留 Pilot 與已有 historical workers，使用獨立 container／checkpoint names。
Installed consumers 的用量與 cooldown 由 durable governor 保存，process restart 不清除帳號已用量。
FinDB internal Source API 的 2400 RPM 為內部服務設定，與 provider 的平台配額分開。
Payload／R2 object／解壓縮／IPC bounds 屬於傳輸與資料處理保護，不列為額外 provider 配額證據。

啟動資格的程式修改位於 `refactor/full-market-baseline-calendar-start`；migration
`3553ed95cfbf` 允許 admission 的 legacy enrollment reference 為 null，保留既有歷史綁定。
所有環境的新抓取均不讀取 readiness／enrollment／expiry／capacity proof。Owner start 只依
published baseline 與完整 published calendar；新抓取另遵守 running control、有效旗標、Source
及 provider API 認證與資料驗證。每次部署仍須確認 installed revision、環境與有效旗標；最新 staging 資料查核見上方現況。
Nasdaq MIC 與 publication 排序修正已隨上述 c744a36 staging release 部署；production 狀態見 deployment runbook。

## 治理資料明確需求

啟動必要治理資料只包含以下兩類，其餘診斷／部署資料不列為啟動前提：

| 資料 | 必備內容 | 驗收方式／目前 staging 狀態 |
| --- | --- | --- |
| Published baseline | 每 provider／dataset 的 release ID、effective date、完整官方成員／provider mapping、來源 URL／SHA256 與不可變原始資料參考、member count／SHA256、Owner publication timestamp／audit | 同 provider／dataset、status=`published`、effective date 不晚於當地今天；staging 七個 feeds 已發布，release IDs 見上表。首份發布提供 `evidence_note` 與 `first_baseline_approved=true`；重大成員異動仍遵循既有 publish 規則 |
| Published calendar | 對應交易市場／當年度的 revision ID、timezone、完整日期與開休市／session times | status=`published` 且年度完整；US／HK／TW／TAIFEX 2026 皆已具備 365／365 published revision |

TAIFEX baseline 成員保留 TX／MTX／TMF／TE／TF、實際月／週到期 contract code／month 與
regular／after_hours sessions，排除價差與 continuous futures。官方成員與 mapping gaps 保留，
不能刪除缺口來宣稱完整。Owner 發布與 start audit 是既有操作紀錄，不新增 receipt／enrollment
等外部啟動核准步驟。

七個 feed 的 baseline 對應如下，每個 dataset 都需要自己的 immutable release：

| Provider | Dataset | Calendar market |
| --- | --- | --- |
| Twelve Data | `us_equity_eod`、`hk_equity_eod` | US、HK |
| FinLab | `tw_equity_eod`、`tw_etf_eod` | TW |
| Shioaji | `tw_equity_minute`、`tw_etf_minute` | TW |
| TAIFEX | `tw_futures_eod` | TAIFEX |

## 設定與啟動

1. Backend 先執行 migration 與 registry seed；設定對應的 `APP_ENVIRONMENT`、Source provider/dataset
   scopes、provider credentials、calendar Serve credential 與 Raw R2 設定。
2. Backend／Fetcher 設定 `FULL_MARKET_ENABLED=true`，選用相同環境的 Full config。部署仍初始 stopped。
3. Fetcher 的 checkpoint 與共用 `FETCHER_ACCOUNT_STATE_PATH` 使用 durable storage；所有已安裝
   Pilot／Full consumers 使用同一帳號 governor。不要刪除或重建既有用量、cooldown 或 checkpoint。
4. 使用 `--sync-universes` 提交官方名單與 provider mapping 的 candidate；它以 authenticated Source
   maintenance 授權執行，無須 enrollment。Owner 發布 baseline，補齊完整 calendar，再手動 start。

Local 範例（repository root 起始；`fetcher/.env` 的 Source、provider、calendar 與 R2
設定須已填入，並與 bounded／historical 命令共用同一個 account ledger）：

```bash
cd fetcher
export APP_ENVIRONMENT=local FULL_MARKET_ENABLED=true FETCHER_CONSUMER_PROFILE=full_market
uv run --env-file .env findb-fetch-full-market --provider finlab \
  --config configs/full_market.local.v1.json \
  --state-path .state/full-market/state.sqlite3 --sync-universes
uv run --env-file .env findb-fetch-full-market --provider finlab \
  --config configs/full_market.local.v1.json \
  --state-path .state/full-market/state.sqlite3 --run-forever
```

首次本機設定依 `fetcher/.env.example` 使用
`FETCHER_ACCOUNT_STATE_PATH=.state/account/governor.sqlite3`，上述 cwd 與其他本機 CLI
相同，皆解析到 `fetcher/.state/account/governor.sqlite3`，由執行者建立且重啟後保留。
所有 providers 的 Pilot／Full／maintenance／historical 共用此檔案，checkpoint 另存；
從其他 cwd 啟動須改用該既有檔案的絕對路徑，不能以不同 cwd 的同字串另建 ledger。
既有 `.env` 須由操作者補上設定：先確認並沿用已存在 governor 的絕對路徑，包括舊範例的
`/private/tmp/findb-local-account/governor.sqlite3`。不得清空或丟棄既有 pins、debt、unknown
liability、cooldown 或 allocation。若要搬遷，停止所有 consumers、備份並完整移轉 SQLite
狀態後一併切換，詳見 [Fetcher 本機設定](../../fetcher/README.md#開發)。本機範本只用於
local；staging／production 路徑與 durable mounts 維持部署設定。

staging／production 使用各自 config 與 durable paths。四個 Full workers 為
`findb-full-market-{twelve-data,finlab,shioaji,taifex}`；Pilot 保留原 names，production Full
checkpoint 保留既有 `/var/lib/findb-full-market/state.sqlite3`。升級只需既有部署驗證，
不需要重新 enrollment、receipt 或 ACK。CLI 保留 `--readiness-file` 相容參數，正常抓取不讀該檔。

舊 enrollment/report/verification 與 repair 工具僅保留歷史診斷用途，仍要求其既有可信檢查；
不是 start 或新抓取前提，正常 worker 不再定期上報 readiness。`ReadinessDeclaration` 舊 schema
不可填造數值，但亦無須為啟動建立 declaration。關閉旗標並 audit stop 可用：

```bash
FULL_MARKET_ENABLED=false uv --directory backend run python scripts/enroll_full_market_runtime.py
```

關閉旗標的一般 bounded deployment 保留已安裝 Full workers/checkpoint，讓 prepared backlog drain，
新 false install 不新增它們。不要刪除 state、container 或改用 Pilot names。待 durable pending plans
皆完成後 operator 可另行退役。Rollback profile 使用 accepted record；bounded replay 保留已安裝 compatible Full image 供 drain，不能用舊 bounded image 降級 drainer；Owner 先在兩個 tabs 停止
Pilot 與 Full，確認 desired/observed stopped，再依既有 rollback 流程恢復。旗標重新開啟不自動 start。

## Dashboard

導入／排程預設 Pilot，`?profile=full_market` 選 Full market；切換只導覽，不寫 control。每 tab
只呈現該 profile 與各自 bulk controls，標籤顯示 running 數量。Full 顯示有效旗標、baseline／calendar blockers、
凍結 scope 與 first dates。Overlap confirmation 使用所有 controls（包含另一個隱藏 tab 的 desired
或 observed running）；Owner、CAS、skip/error UI 與部署前停止兩邊提醒維持。

「營運監控 → 交付監控」預設開啟「全市場計畫」。資料集下拉選單由全部既有計畫去重產生，
可查近期沒有計畫的歷史資料集；交易日為選填，每頁 25／50／100 筆。表格顯示完整性、截止時間、
預期／有資料／正常無資料／缺漏／受阻；展開可查看 universe release 與缺口原因。
缺口最多顯示 1,000 筆，已列出筆數與總缺口數分開標示。正常無資料須有持久化證據，不視為錯誤。
此區塊為唯讀核對，不啟動抓取或自動建立回補；「缺漏告警」追蹤資料集／日期層級異常，
「歷史回補」保留 operator／owner 的預覽與確認操作。
分頁深連結、刷新與錯誤處理見 [dashboard.md](dashboard.md)。

## Production 啟用準備更新（2026-10-13 05:03 UTC）

長期 Source credentials 已配置完成：Twelve Data US／HK、FinLab equity／ETF、Shioaji equity／ETF
與 TAIFEX 七個 feed 的實際 Fetcher-role HTTPS 查核皆為 200；四個市場的 published calendar
皆完整 365 日。TAIFEX Source client `01a13364-4387-7b9b-b5e0-411c745002e3` 為 active、無 expiry，
secret AWSCURRENT 為 `6145ed3d-beb6-40bb-8516-2eb0d34ecee7`。三個既有 provider keys／IDs
及 calendar credential 保持原身份，Source RPM 為 2400；跨 FinDB／Fetcher 的 secret read 均拒絕。
查核證據為 SSM `1936dad3-82dc-4f44-a05a-314858301ca3` 與
`1bccffe5-9f3e-4864-afb0-d56bab798a9f`。TAIFEX secret metadata apply 為 1 add、0 change、0 destroy。

Production 全部 Pilot stopped／revision 9、四個 Full stopped／revision 3，沒有 active historical
request／item／lease；六個舊 bounded acquiring processes 尚待正式 drain／upgrade。Staging 四個
Pilot desired running，Full 皆 stopped，另有一筆 queued FinLab historical、未持有 lease。
四個 Full workers 正式部署與七個 native provider feeds 的資料驗收仍未完成；不可把 credentials
或 Owner start 通過視為 workers／實際資料已完成。

舊 production 配置指向 `/var/lib/findb-account/production/governor.sqlite3`，但六個 containers
沒有 host account mount。SSM `82ded678-03ae-4c3f-ad63-a61c18d8afa4` 唯讀確認 host 與六個
container 的該 governor 路徑皆不存在，因此此次沒有該路徑的舊 governor debt 可搬移。
已存在的 provider checkpoints 必須保留；這項結果不能推廣成其他環境可忽略舊 governor。

## 用量與傳輸保護

正常抓取使用 `platform_limits.py` 中的已確認限制。Twelve Data 按 1597 credits／分鐘 pacing；
Shioaji 按 50 行情請求／10 秒 pacing，並在 Kbars 前查 `api.usage()`，每日達 500 MiB 就拒絕抓取。
FinLab 由 authenticated SDK API 執行每日 5000 MB 配額；detached dataframe bytes 只用於傳輸
驗證，不當作 API network usage。TAIFEX 不填造數值配額，429 保留 account cooldown。
Source API 依 DB-backed credential 的實際 rate limit 執行 429，舊 allocation 不另加 Source 預算。
Full governance client 遇到 429 時依 `Retry-After` 秒數或 HTTP 日期等待；無效值使用既有指數
backoff，等待受 `FETCHER_MAX_RETRY_AFTER_SECONDS` 限制，嘗試次數受 `FETCHER_MAX_ATTEMPTS` 限制。

已安裝的 account ID 可沿用以保留 ledger，provider／credential fingerprint 與 ledger ID 對應
會以 SQLite transaction 持久化；完成首次 pin 後，舊證據檔移除或修改不改用新 ledger，
既有 debt/cooldown 不重設，也不輸出 credential。Twelve Data fingerprint 去除前後空白；
FinLab 1.5.7 gateway、identity 與 installed allocation 比對均使用 `credential.strip()[:64]`，
符合 native `LoginPanel.login`／`get_token` 的前 64 字元選取，長 token 的後綴不另建帳號 ledger。
同一有效 credential 的空白格式變動不另建 ledger。Full／Pilot／historical Shioaji 的 key／secret 與 SDK 輸入一致去除前後空白；
只合併可由同一有效 credential 證明關聯的 pins：FinLab 包含明確提供與 SDK 等值 installed
token 的 raw、舊去空白與新 SDK 等值 fingerprints；未知或不同實際 key 不合併。Request debt
加總、cooldown 取最大、alias merge 冪等，原 ledger 與 audit 保留；不合併不明身份的 orphan。
首次升級尚未建立對照時，若舊 allocation 遺失、損壞或沒有可用 account ID，舊 schema 沒有
provider／credential 關聯可供還原。此例外以 credential fingerprint 建立新 ledger，只依平台
實際配額；原 ledger 用量與 cooldown 保留供稽核，不猜測、改標籤或合併匿名債務。
此操作接受舊用量與 cooldown 無法接續的風險；應在首次升級前保留可辨識的 allocation 與
governor 以維持完整接續。不同環境不設分配或預算，平台累計用量與服務端限流仍包含外部用量。
Governor 保留用量、pacing、cooldown；舊 capacity violation/repair 資料保留稽核，正常抓取不再
以它們作前提，也不刪除既有 debt。HTTP／SDK／IPC／R2 大小、格式與 provenance 驗證仍保留；
過大或不合法 response 不得變成成功 payload，但不新增永久 account capacity 封鎖。

Account acquisition protocol v2 對同一 governor 路徑／account 使用跨 process OS advisory lock
及 process thread mutex，從 durable preparation 持有至 operation 完成／durable finalization。
SQLite transaction 只做短暫 ledger 更新，不跨 network／SDK 等待；其他 account 可以同時執行。
HTTP 在 transport send boundary、FinLab 在 `data.get`、Shioaji 在 child `api.kbars` 記錄 actual start。
Twelve Data HTTP boundary 使用該 client 實際 config credential（保留 raw／normalized pin 合併），
即使環境變數指向另一個 key，account lock、request debt 與 cooldown 都跟隨實際送出的 key。
FinLab 1.5.7 的 headless `login(token)` 只選取 process-global token，不算上游 acquisition。
所有 `FinLabSdkGateway` 以 process-wide SDK lock 保護 login、最後授權、`data.get` 與 finalization；
SDK lock 先於 account lock，不同 explicit tokens 在同 process 依序使用，拒絕授權會釋放 lock。
Account identity 在 login 前建立；SDK 修改的 token environment 在離開 scope 時還原，避免
installed allocation 的環境身份被改寫，成功、拒絕或 SDK error 都遵守此規則。`from_env`
及 central account identity 使用同一 SDK scope 的 installed environment 快照，跨 thread 等待
還原完成；同 thread authorization／data／finalization callback 讀取 scope 前的 token，
不會把暫時 SDK token 當作 installed allocation，也不重入 SDK 鎖。
同 thread 的 recursive gateway dispatch 拒絕，避免授權 callback 改寫外層 token 或造成 deadlock。
持有 account lock 的 callback 僅可唯讀取得全部相關 fingerprints 已 pin 至同一 target 的 identity；
需要建立／合併 pins 時明確拒絕，保留債務，離開 operation 後可正常合併。唯讀路徑不再取得
identity mutex，避免其他 process 正在等待該 account 的 merge 形成反向等待。外部 account lock
內的巢狀 SDK dispatch 拒絕；installed snapshot 只嘗試立即取得 SDK lock，busy 時明確拒絕。
Known requests 原子且冪等計入 actual UTC minute／day；正常拒絕或過期 grant 不增加 known request debt。
下一次開始不得早於前次 completion 加平台 interval，可能比 grant-based pacing 慢。
Cached FinLab bundle／SDK catalogue 不算行情 acquisition；TAIFEX cached report 不另預留 orphan quota。

Durable armed intent 必須在最後 Source authorization 前保存。Current control／credential／scope
授權回應的 `checked_at` 與 HTTP header 相符，server check age／整次 RTT 均小於 5 秒；
immutable header reception origin 在 response hooks／body decode 前取得，local dispatch／child GO
期限最多 50ms，並受 actual minute boundary 與 historical lease expiry 限制。等待、準備延遲、rollover
或 token 過期須重新檢查 quota／Source，最多三次後 fail closed；不能把 child GO 發送時間當成新 origin。
這是有界 fresh check，遠端 stop 與 provider invocation 不構成跨服務 atomic transaction。
Historical 每 item 使用 protected lease／cancellation probe，不延長 lease。

HTTP 429 的 headers 一到就 durable MAX 保存 cooldown，先於 body 讀取。Provider `Retry-After`
秒數／日期採最少 60 秒，最多 `FETCHER_PROVIDER_MAX_RETRY_AFTER_SECONDS`（預設 300，允許 60–86400）；
此 cap 在 armed intent 預先保存。Crash 尚無 actual-start 證據時，下一個 lock owner 將 armed 轉 unknown，
從 recovery 起至少等待該 cap，並在 armed→recovered 可能區間重疊的 quota windows 各保留一筆 liability。
不偽造精確 start，不重設歷史債務；已知 finalization 不重複計數。Persistence 失敗或無法終止的 SDK
child 保留 lock，直到 process 結束與後續 recovery，避免其他 consumer 越過未確認操作。

Shioaji SDK child 由明確 allowlisted environment 啟動，provider keys 僅由 private pipe 傳入；
argv／environment／READY-GO 不包含 Source credential、cloud 或其他 provider keys。
Parent 作 Source check，child 收到共享 lock FD、key-free GO 與最多 5 秒前的 `api.usage()` sample，
在 actual `kbars` 前再檢查 freshness／lease／minute／500 MiB。Parent SIGKILL 後 child 仍持有相同 lock FD，
SDK completion 的 quota／usage／cooldown 先 durable finalize，再交付結果。

SDK scalar observations 保存 sample／observation timestamps、bytes／limit／remaining。官方 counter
按交易日台北 08:00 reset，SDK 沒有可證明的 epoch 欄位；跨該可能 reset boundary 重新取樣，
不推定 UTC quota day 為 counter epoch。當前 fresh authoritative counter 達 500 MiB 或 unavailable／
inconsistent 時拒絕新 Kbars。Crash 後重新查 current counter；歷史 reset／unknown bytes 與 legacy
cumulative floors 只保留稽核，不加上 counter delta 重複計費，也不用 response／IPC 大小捏造 API byte bound。
部署與 rollback 的全 consumer 升級要求見 [deployment.md](deployment.md)。

首次 Full checkpoint bootstrap 僅傳入與 config 一致的 `APP_ENVIRONMENT`，在 local／staging／production
以離線 `--initialize-state` 建立 SQLite；不載入 Source/provider secrets。已有 checkpoint 跳過初始化。
Full profile 的此步驟僅套用 `full_market` consumer 與 `findb-fetch-full-market` CLI；
Twelve Data／FinLab Pilot 與 historical 保留各自 startup/state 邏輯，不能收到 Full 初始化參數。
未指定 consumer 維持 Pilot 預設，Full CLI／consumer 不匹配一律拒絕。Shioaji Pilot 的既有專用
CLI bootstrap 與 versioned-state guards 保留。Coordinator 保留用於 compatible image／secret selection 的
effective Full profile，另將已驗證 manifest 的 recorded release profile 傳給 bootstrap dispatch。
Full command 明確傳入同一 mount 的 `--state-path`：production 保留
`/var/lib/findb-full-market/state.sqlite3`，staging 使用 `/var/lib/findb-full-market/staging/state.sqlite3`，
initializer／control probe／worker 都依此定位，不以 CLI default 代替 checkpoint。
已有 named／legacy Full worker（包含僅剩 accepted `-previous` 的 recovery container）時，無論 ordinary
upgrade 或 accepted replay、bounded 或 Full profile，checkpoint 必須先存在、符合既有 nonsymlink path 規則，並通過 read-only 最小穩定 core-table
檢查（不要求新的 capacity／repair tables）。以 read-only `PRAGMA integrity_check` 驗證實際 pages，
並確認 `full_work`、`full_plan`、`full_quota`、`full_cursor` 的完整舊 core 欄位可讀；僅有同名空表不算合法
checkpoint。Zero-byte、physical corruption、缺少必要欄位、非 Full 或 malformed SQLite 同樣拒絕，
即使 retained helper／image 較舊也不得先補建 schema。此唯讀 inventory gate 在 historical worker
retirement、其他 stop／rename／start、helper、runtime secret loading 或 control probe 前執行；不建立
空白替代 state，也不嘗試恢復遺失 prepared work。Valid previous-only recovery 保留原 container identity、
image 與 checkpoint，由既有 recovery journal／helper 接續恢復。Pilot、Full 與 historical 共用完整交易 snapshot；journal 記錄原始
stable／previous 名稱、ID、image 與 running 狀態；失敗或 signal 會回復原始狀態，原本 stopped 不會被
自動啟動。若 stable 另有 accepted stale previous，先以獨立 journal 移至 transaction backup；失敗先回復
current original，再回復 stale backup 的原始名稱與狀態。只有成功 commit 後，且 backup identity 相符、
graceful stop 與 strict retirement 均通過，才以非 force removal 清除 stale backup。若 Docker 持續拒絕
stop，交易明確回報 rollback failure 並保留原始 identity，不 force-remove；此時不能宣稱已恢復原始
stopped 狀態，須 operator 排除外部 Docker 問題。
Bounded accepted replay 的 drainer image 取自完整 registered accepted original snapshot；unaccepted
stable interrupted candidate 不參與 image 選擇。Legacy 名稱的 migration 也先選 accepted original，
再依該 original 的 command 判斷 Full 角色；未 accepted 的 Pilot／Full stable 不會遮蔽 accepted Full previous，
也不會將此 previous 誤判為 stale backup。Previous-only historical 的 accepted original 在建立
replacement 前停止並嚴格退役，失敗／signal 依 journal 回復原 previous 名稱與原 running 狀態。
各 profile 的 registration 先完整讀取 stable／previous 的 accepted metadata、ID、image、running 狀態，
並檢查 transaction-backup collision，全部成功後才登記 recovery journal 與 staged rename。Inspect／
collision 失敗不會啟用 generic rollback；每個完整記錄同時保護兩個 accepted initial IDs（不將未 accepted 的 interrupted candidate 當 original），因此 journal 已登記、
backup 尚未 rename 時收到 signal，也不會將 accepted stale previous 誤刪為 candidate。舊 accepted
runtime 沒有 label 的 Docker `<no value>` 表示仍相容；inspect command failure 或未知 metadata fail closed。
Coordinator 初始 inventory 同時涵蓋所有將操作的 Pilot／Full／historical stable、previous、candidate、
backup 與 preflight names，並驗證完整 identity/state 欄位；任何 unknown 在 journal、current pointer、
secrets 或 runtime mutation 前拒絕。Normal helper retirement 與 commit preparation 都要求成功且合法的
Running=false、ExitCode=0、OOMKilled=false 與空 State.Error；僅保留既有精確匹配舊 staging historical
137 的退役例外。所有可失敗的 original／backup identity、state 驗證先於 commit；成功後僅以
已驗證 ID 做 non-force removal，避免 name reuse，亦拒絕已重啟的 runtime。刪除前再次依 ID
驗證 image 與 strict retirement 欄位；postcommit 查詢／狀態失敗會回報 `committed_cleanup_failed`（`committed=true`），保留已 committed replacement／checkpoint 與尚存舊 IDs，不回報 activated success、
不宣稱 rollback，須先排除 Docker／舊 runtime 狀態問題再退役。
Trusted external guard 與 coordinator 使用相同 managed inventory：Full／Pilot 及兩者 historical base，
各自涵蓋 stable、previous、candidate、transaction-backup、legacy-previous、preflight、preflight-state、
preflight-control。Standalone guard 使用一次成功 exact-name inventory snapshot，每個列出項目仍須成功 inspect；
衍生／historical 名稱也必須在 immutable coordinator 前通過 command 與 Full checkpoint gate。
Current external gate 與 coordinator 的 `{{json .Config.Cmd}}` 必須是非空 JSON argv array、每項為字串，
並以已知 managed worker executable（Full、Pilot、historical）或確切 `python -m findb_fetcher` readiness
command 辨識角色。Blank／non-JSON／null／未知 command 均在 journal、pointer、secret 或 runtime effects
之前拒絕；argument 內的 Full 字樣不決定角色。查詢或 decoding 失敗只回報固定安全 reason，不輸出 Cmd。
Current external gate／coordinator／helper 只將成功 `docker container ls --all` 的 exact-name inventory
視為不存在的證據；列出的 runtime 還須成功 inspect。Docker rc 1 同時可能表示 daemon／permission
failure，不能以 inspect 非零推定 absence。Unknown query 在 journal／runtime mutation 前拒絕；已開始
交易則明確進入 rollback，rollback／recovery 本身無法確認 inventory 時回報失敗並保留未知 identity。
Recovery 的 `.State.Running` 查詢須同時成功且回傳 `true`／`false`；rc 1／81、空字串或未知值均不得
視為 stopped 或用來猜測 start／stop。Named／legacy／generic cleanup 與 strict retirement 欄位查詢
不依賴 Bash errexit，conditional caller 也必須明確回報 `transaction_rollback_failed`／`recovery_failed`。
查詢無法驗證時保留 original name／ID／image，不宣稱已還原 running 狀態。Identity／image 與
舊 staging historical 137 例外的 image／Path／Args／Cmd／accepted 查詢也必須先驗證 exit status；
即使 stdout 符合已記錄值，非零 status 仍拒絕 restore、退役例外或 commit cleanup。
Genuine first install 與 previous-only recovery 仍支援。Helper entry 的 interrupted candidate／unaccepted
stable／bootstrap／historical 清理也使用 graceful strict retirement，絕不 force-remove；所有支援 consumer 的 stable 與
accepted previous 並存時，parent 必須先 journal/stage backup，helper 拒絕未 staged 的 accepted backup。
`release_fetcher_provider.sh` 是 verified coordinator 的內部 child；operator 必須使用已驗證的 coordinator／workflow，
直接呼叫 helper 不屬支援的維運介面。Parent 的 initial inventory、完整 journal 與 checkpoint gate 是部署前提。
實際 provider helper recovery trap 也採 graceful stop、stopped／exit 0／無 OOM／無 Docker error 的 strict
retirement，再以非 force removal 清理新 candidate／replacement／historical worker。Known original ID
不進入此清理流程；retirement 拒絕則保留該 runtime 並明確回報 recovery failure，不能由 child force-remove
後再期待 coordinator 還原。

GitHub replay 的外層 checkpoint gate 使用 `infra/deploy/check_full_market_checkpoint.sh`，另從同 repo 的
`github.workflow_sha` checkout 取得；必須驗證 checkout SHA 與 guard 存在，缺檔或不符即停止，不能 fallback
到 `inputs.revision` 或舊 accepted bundle。SSM 對 base64 傳輸解碼後再次校驗 SHA-256，先執行此唯讀 gate，
再以 `app-environment-v1` marker gate 確認 coordinator 契約後進入 immutable coordinator。pre-marker
accepted bundle 在此被拒絕，不再是 replay 目標；帶 marker 的舊 bundle 仍使用其記錄的 profile，
manifest／bundle 內容不改寫。既有 accepted bundle／image 可正常 replay，
checkpoint 遺失或不合法時需 operator 處理 durable state，部署不會重建未知 prepared work。
目前 transported 離線測試使用 frozen coordinator／helper／CLI excerpts 與 no-op registration，證明
拒絕順序及舊 bootstrap 邊界，未完整覆蓋舊 coordinator 的 flags／traps／pointer 與成功 replay。
另有完整 HEAD coordinator 的 unknown Cmd proof，確認 current guard 拒絕時零 effects、IDs／pointer 保留；
此證據不等同完整 accepted bundle 成功 replay。完整離線 harness 見 backlog，實際成功 replay 仍屬外部驗收。
Docker `--check`／`--require-stopped` probes 可使用 `cache_dir=-`，空 cache mount 在 Bash 3.2 下同樣安全。
`--require-stopped` 僅以 read-only SQLite 驗證舊 compatible checkpoint 也具備的 core tables，再查詢
Source desired state；缺檔／不合法 state 會在載入 credential 與 Source 呼叫前拒絕，既有 prepared
bodies、debt、cursor 不改寫。只有沒有既有 Full worker 的真正首次 Full 安裝可執行明確 initializer。

所有 HTTP status 使用 bounded raw-body read 與 provenance／大小驗證；headers 的已知 429
先 durable MAX 保存共享 account cooldown，再讀 body。實際 invocation journal 決定 request debt，
不使用 detached Permit 預先預留行情請求；body／encoding／observer failure 仍保留已開始的 request
及 cooldown。Twelve Data daily／catalogue、官方 universe、TAIFEX fallback report 的非 200 回應
與中途 transport failure 都不得解析為成功 data／universe。201／206 同樣拒絕；錯誤 JSON 只供
遮蔽 secrets 的診斷。HTTP／IPC body 長度是傳輸驗證 evidence，不能代替 Shioaji SDK network bytes，
無 counter 時記 unknown／unavailable，不虛構 0 bytes 或退款。Prepared Source drain 保持可完成。

SDK bootstrap 也屬受保護 operation：dormant child 在 account lock／armed intent／FD ownership
與最後 current Source GO 之前，不建立 SDK instance、不 login、不查 lazy contracts。每次 construction、
login、TSE／OTC group、Kbars 分開授權／finalize；cached detached catalogue 不新增抓取。
Kbars 在首次 durable finalization 記錄實際 SDK 呼叫的成功／錯誤；已開始但失敗的 Kbars
仍計一次 request，後續錯誤處理不覆寫已 finalized 的結果，logout cleanup 維持 zero-unit。
所有入口的 account identity 取實際 SDK key 正規化後的 fingerprint，顯式 key B 不套用 environment
key A 的 pins、allocation、cooldown 或 login budget。已知 raw／normalized pins 只按可證明關聯合併，
含 `_`／`%`／backslash 的合法 account IDs 以 literal prefix 保留 allocation rows，不匹配其他 account。

Shioaji 官方最多 1000 login／day；此 runtime 使用**本地保守 rolling 86400 秒最多 1000 actual login
starts**，不假設官方 login reset epoch。`operation_kind=sdk_login` 的已知 start（包括 SDK 登入失敗）
各占一筆；crashed armed login 以可能區間重疊保留一筆 unknown liability；cancel／stale GO 不算
已知 login，其他 bootstrap／Kbars／cleanup 不占 login 次數。Operation ID 與 alias reassignment
使 finalize／recovery 冪等；不可重設已知歷史或把它變成所有 Full new acquisition 的日預算。
首次尚未登入時 api.usage 不可用，保存 `not_authenticated`，只允許 fresh Source 授權的 bootstrap；
login 後必須取得 consistent、fresh current bytes／connections，才可查 contracts／Kbars。

SDK official usage observations 保留 `connections`（舊資料為 null，不造 0）、sample timestamps、
bytes／limit／remaining。Authenticated connections 須為 1–5；已知 fresh connections≥5 拒絕新的
login／reconnect，既有有效 session=5 可沿用。首次／無 fresh observation 的平台拒絕仍記一筆
known login 並 bounded cleanup；不建立 person_id admission ledger 或捏造不同 keys 的 person_id 關聯。
Native 驗收須保存 official connections、登入拒絕與 cleanup evidence；目前仍待正式 deployment／native acceptance。

Logout 是既有資源 cleanup，Source stopped／revoked 不阻止關閉；它不發起 login、contract 或 Kbars。
Same-account lock／zero-unit cleanup intent 保護至 durable completion；crash 保留 cleanup unknown
cooldown，不捏造行情 request 或 SDK bytes。Parent timeout／EOF／SIGKILL 期間 child 保留共享 FD，
先 finalize／cleanup 再交 ACK／釋放；無法終止的 child 留 lock 供後續 recovery。


舊 production Full runtime 曾使用 Pilot 的 `findb-fetcher-*-scheduler` names。升級或 flag-off bounded
部署會先 graceful stop，嚴格驗證非 OOM、無 Docker error 與合法 retirement exit，再搬至獨立 Full
names；既有 accepted image/identity/checkpoint 不刪除。每次 stop/rename 前登記交易 recovery entry，
失敗或 INT/TERM/HUP 時先倒序恢復 provider，再恢復原 names 與原 running/stopped 狀態。若有 accepted `-previous` 殘留，先停止／嚴格驗證並搬至獨立 Full `-legacy-previous` backup，避免 Pilot 復原誤選舊容器；失敗先恢復 current original，再恢復舊 previous，成功才清除這個非 current backup。退役失敗
會中止交易，不能透過強制刪容器跳過。Owner 應先停止兩個 profiles；idle `--run-forever` process
仍 running 是受支援的升級情境，deploy helper 負責優雅停止。
