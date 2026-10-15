# Pilot／Full market 共用執行流程

`FULL_MARKET_ENABLED=false` 是 local、staging、production 的預設值。Backend 與 Fetcher 必須使用
相同 `APP_ENVIRONMENT=local|staging|production` 與旗標。JSON `deployment_target` 仍是環境匹配契約。`FULL_MARKET_ENABLED` 從 AWS 部署控制設定載入，
只控制 Full market 新抓取資格；兩環境固定安裝相同 runtime，不以旗標選擇程式。
旗標為 true 不會啟動排程。Full market 的啟動資料條件只包含已發布 baseline。
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

## Profile 與計畫隔離

`findb-fetch-runtime --execution-profile pilot|full_market` 是兩環境共同入口。
Pilot 每條 feed 取 published universe 穩定排序的前兩個 canonical 商品；期貨以 product／contract 選擇，
保留入選商品 sessions。Daily plan 凍結選取結果，不因重試、補抓或 universe 更新改寫。
兩種 profile 各有 controls、first dates、plans、checkpoints、缺漏與告警；同環境共用 account governor。
只追補各自首次日期後的缺口，prepared delivery 在停止後仍可完成。

`expected = data + no_data + missing + blocked`。Data 必須有該 plan 所屬 run 的持久化 canonical 寫入證據，
Pilot 成功不能直接完成 Full plan。Minute 必須有完整 sequenced snapshot；DQ error、空批次、quota failure
不得算成功或 no_data。No_data 必須有 durable provider evidence。

## 啟動契約

Pilot／Full market 每個 feed 只要求同 provider／dataset 有 `published` official baseline，且 effective date
不晚於交易所當地今天。至少一個 feed 符合即可啟動，並只凍結符合條件的 feeds。日曆不必先設定。
Enrollment、installation receipt、readiness ACK／expiry、account／capacity proof、環境旗標與
registry 額外 metadata 不列為啟動資格 gate。舊 `full_market` metadata 若為字串或 list，
start 視為缺少 optional governance，重新建立 activation date；有效 contract declaration 與
既有 immutable first date 仍保留。未知 scheduler／scope 仍依 API definition validation
處理，Owner 操作權限與 CAS 保留。

Owner 透過 `PATCH /api/v1/admin/schedulers/{scheduler_key}`，傳入 `desired_state=running`
與 `expected_revision`。Backend 在同一 transaction 重查 baseline 與當時可用的 calendar metadata，凍結 ready datasets、
baseline IDs、可空的 calendar revision IDs 與首次日期，並寫入操作 audit；admission 不必綁定 enrollment。
新的 ready sibling 須停止後再啟動，重複 running PATCH 不改凍結範圍。
每 dataset＋profile 首次日期由交易所當地今天決定；migration 保留合法 legacy activation_date，malformed
legacy 日期在首次 start 時以 immutable first date 修正。停止、重啟與重新 enrollment 均不重設日期。舊 `/admin/feeds/{dataset}/activate`
與 `/deactivate` 在 Owner 認證後回傳 410；沒有 per-feed activation、manual readiness_approved 或五日門檻。

每次 provider call 前立即查 effective Source authorization。Full market 旗標關閉（不影響 Pilot）、Owner 停止、Source credential 撤銷／到期時，禁止新抓取與 plan。已保存 prepared payload／frozen plan 的 delivery、
normalization、canonical 與稽核可繼續；不重抓已 prepared 資料。關閉旗標由 ingest/all lifespan 或
下述管理命令停止 Full controls 並寫 audit；GET 投影永遠唯讀。重新開旗標後仍需 Owner 手動啟動。

## 有效日曆與每日政策

Backend 是政策權威。`runtime/configuration` 使用四個獨立開關：
`TWELVE_DATA_SKIP_NON_TRADING_DAYS`、`FINLAB_SKIP_NON_TRADING_DAYS`、
`SHIOAJI_SKIP_NON_TRADING_DAYS`、`TAIFEX_SKIP_NON_TRADING_DAYS`。
每個開關皆為 `true`／`false` 字串，預設全部 `false`，兩環境欄位相同，經部署生效。
Fetcher 透過 Source 取得政策與每日決策，不保存另一份日曆或政策設定。預設每日執行，包括週末／休市；
true 才略過非交易日。排程時間、交易所時區、Owner controls、平台配額、429 與既有重試規則仍有效。
不設連續十筆失敗中斷規則。

有效日曆逐日優先讀 published revision；缺市場、年份或日期採週一至週五開市／週六日休市，draft 不參與。
TW／US／HK／CN／TAIFEX 使用固定市場時區及正常收盤時間，published 提早收盤優先。
查詢不寫 DB、不自動發布；API 保留 `calendar_source=published|weekday_default` 與可空 revision。
缺日曆可預設，DB／認證／通訊故障則正常報錯。部署與 Owner start 不 seed 或檢查 calendar completeness。

`daily_execution_decision` 以 provider／dataset／日期／profile 唯一識別，原子保存 planned 或 skipped_calendar，
凍結當日 calendar／session bounds／policy hash。既有 plan、成員、first dates、prepared bodies 不因後續更新改變。
`skipped_calendar` 是最終決策，政策變更不重開；可另建受當時政策約束的人工回補。
Source 仍拒絕未來日期、超前 fetched_at 與未結束 session；非交易日使用正常收盤時間。

人工回補在建立時凍結所有日期，略過項目標示 skipped_calendar；全略過的 request 完成但不產生成功 run。
Claim 不重算政策；可先建立當日請求，但必須到凍結快照的市場收盤時間才發給 lease，
每次新抓取授權也核對同一界線。Published 提早收盤及非交易日正常收盤皆使用建立時快照，
後續日曆修訂不改變既有工作的界線；已準備的交付可繼續完成。
既有失敗／過期與序列執行規則保留。略過不計入 expected、missing 或 no_data。
實際非交易日回應依原有 canonical evidence、完整 minute snapshot 與 DQ 判定，空回應不等於成功。
Migration `91a4c7e2d630` 保留既有計畫及歷史項目，用升級當時有效日曆建立 `legacy_migration_snapshot`，
不聲稱它是原始建立時的日曆；不補入舊回補範圍中原本省略的日期。這是新的 rollback 下限。

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

Raw R2 以內容雜湊決定 immutable object key，使用 `If-None-Match: *` 建立，避免重試覆寫
已鎖定物件。409／412 不直接視為成功；必須在同一 bucket/key 讀回完整且有大小上限的 bytes，
核對長度、內容、content type 與 provider/dataset/symbol/SHA-256 metadata 後才可重用。
讀取失敗或內容不一致仍阻擋交付，查無物件的競態可重試；不移除 bucket lock 或刪除物件。
Twelve Data 的 catalogue 與實際日資料回應共用 Nasdaq MIC 規則：僅 `XNCM`、`XNGS`、
`XNMS` 對應官方 `XNAS`，其餘交易所與幣別仍須完全符合 frozen universe；原始回應不改寫。
Twelve Data universe refresh 只抓取 Source 當次授權的美國／香港 country catalogue，
不下載全球 `/stocks` 目錄。每個市場各自經過 dataset-bound fresh authorization 與 account governor；
回應大小上限維持不變，Raw 與 evidence URL 按市場分開，API key 不寫入 evidence URL。

Source 的 `POST /api/v1/source/universes` 使用 16 MiB nginx 與 Backend Content-Length 上限，
容納完整市場成員；其餘 Source 端點維持 1 MiB nginx 上限及原有
`SOURCE_MAX_PAYLOAD_BYTES` Backend 限制（預設 1,000,000 bytes）。nginx 同時限制 chunked
body 的實際大小。Universe 端點使用相同 IP allowlist、proxy 與 Backend
Source 認證，仍驗證 dataset scope、最多 30,000 個成員與 evidence；上限不是略過驗證的權限。
中途停機、撤銷某市場權限或任一回應失敗時，不提交部分 catalogue evidence。

這些程式保護不等於 native 驗收通過。驗收仍須包含真正 locked object 的重複寫入、七條 feeds
各自的非零 canonical／Serve 證據，以及 profile、plan、run 與 R2 bytes 的對應。
Shioaji 共用 runtime 明確套用既有 `SHIOAJI_SIMULATION=true` 驗證，再建立隔離 SDK；
Pilot／Full market 均使用資料擷取用 simulation，catalogue raw evidence 也記錄相同模式。
不合法設定在 child process 建立前拒絕，不能默默回退至 live-account mode。

啟動資格的程式修改位於 `refactor/full-market-baseline-calendar-start`；migration
`3553ed95cfbf` 允許 admission 的 legacy enrollment reference 為 null，保留既有歷史綁定。
所有環境的新抓取均不讀取 readiness／enrollment／expiry／capacity proof。Owner start 只依
published baseline；日曆缺少時使用週一至週五預設；新抓取另遵守 running control、有效旗標、Source
及 provider API 認證與資料驗證。每次部署仍須確認 installed revision、環境與有效旗標；最新 staging 資料查核見上方現況。
Nasdaq MIC 與 publication 排序修正已隨上述 c744a36 staging release 部署；production 狀態見 deployment runbook。

## 治理資料明確需求

啟動必要治理資料只包含 published baseline，其餘診斷／部署資料不列為啟動前提：

| 資料 | 必備內容 | 驗收方式／目前 staging 狀態 |
| --- | --- | --- |
| Published baseline | 每 provider／dataset 的 release ID、effective date、完整官方成員／provider mapping、來源 URL／SHA256 與不可變原始資料參考、member count／SHA256、Owner publication timestamp／audit | 同 provider／dataset、status=`published`、effective date 不晚於當地今天；staging 七個 feeds 已發布，release IDs 見上表。首份發布提供 `evidence_note` 與 `first_baseline_approved=true`；重大成員異動仍遵循既有 publish 規則 |

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

Backend 先 migration、registry 初始化及 Source scope 校準。Owner 發布 baseline 後才人工啟動。
Staging 常態只開 Pilot，新版驗證時短暫開 Full；production 常態只開 Full。部署本身不 start controls。

```bash
cd fetcher
export APP_ENVIRONMENT=local FETCHER_ACCOUNT_STATE_PATH=.state/account/governor.sqlite3
uv run --env-file .env findb-fetch-runtime --execution-profile pilot --provider finlab \
  --config configs/full_market.local.v1.json --state-path .state/pilot/state.sqlite3 --sync-universes
uv run --env-file .env findb-fetch-runtime --execution-profile pilot --provider finlab \
  --config configs/full_market.local.v1.json --state-path .state/pilot/state.sqlite3 --run-forever
```

Full 使用 `--execution-profile full_market` 與獨立 checkpoint，並將 Backend／Fetcher 的
`FULL_MARKET_ENABLED` 同步設為 true。各 consumer 共用同一 governor 絕對路徑；不能把 checkpoint 與
account DB 指向同一檔。既有 governor 的 pins、debt、unknown liability、cooldown 不得刪除或重設。
維護同步以 Source maintenance 授權執行，不需要 enrollment；新抓取仍要求 published baseline 與 running controls。

AWS 路徑、首次切換、備份與回退下限見 [deployment.md](deployment.md)。CLI 的 `--check` 不呼叫 provider，
`--initialize-state` 只允許新 installation 建立 schema，既有 checkpoint 遺失必須阻塞。
`--require-stopped` 唯讀驗證正確 profile checkpoint 與 controls，不能藉部署修改 desired state。

## Dashboard 與 API

Overview／交付監控以 `profile=pilot|full_market` 分頁。Staging 預設 Pilot，production 預設 Full market。
各自呈現數量、缺口、告警、首次日期與啟動狀態；URL、篩選、刷新及 cache key 均帶 profile。
未指定 profile 的既有 plan API 維持 Full market 語意，新 runtime／Dashboard 必須明確傳入。
細節見 [dashboard.md](dashboard.md)。

## 用量與傳輸保護

正常抓取使用 `platform_limits.py` 中的已確認限制。Twelve Data 按 1597 credits／分鐘 pacing；
Shioaji 按 50 行情請求／10 秒 pacing，並在 Kbars 前查 `api.usage()`，每日達 500 MiB 就拒絕抓取。
FinLab 由 authenticated SDK API 執行每日 5000 MB 配額；detached dataframe bytes 只用於傳輸
驗證，不當作 API network usage。TAIFEX 不填造數值配額，429 保留 account cooldown。
FinLab SDK 的完整歷史 DataFrame 仍計入既有回應大小上限，但以每批 16 個欄位計算
與完整 `orient=columns` JSON 相同的 UTF-8 bytes，避免同時持有整份 JSON 字串與 bytes 副本。
全域重複欄位／索引仍在 accounting 前拒絕；完整大小只回報一次，之後才選取目標日期與商品。
此計算不改 provider 配額、下載請求、回應上限或凍結計畫，也不保證 SDK 本身載入 DataFrame 的記憶體足夠。
Gateway 對 FinLab 1.5.7 原生 FileStorage 設定 `use_cache=False`，只釋放記憶體中的
DataFrame references，保留既有磁碟檔案與到期 metadata；不呼叫會刪檔的 `clear()`。
SDK scope 內同時處理呼叫前後的實際 storage 與 package re-export，避免切換 storage 後
舊物件仍保留整份歷史資料。純記憶體 CacheStorage 與自訂 storage 不變，避免清除唯一副本而增加下載。
有效且未變動的檔案仍由磁碟命中；外部更新／過期時遵循 SDK 的 recent merge、hash 檢查及完整下載 fallback，
所有網路使用仍受原有 acquisition／governor 管理。這項調整不保證單一大型 frame 或 CacheStorage 的記憶體上限。
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
所有 `FinLabSdkGateway` 以 process-wide SDK lock 保護 login、最後授權、`data.get`、finalization、
完整回應計量、目標投影與 FileStorage 記憶體快取清理；Acquisition 仍在 `data.get` 後完成，
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
部署 inventory、checkpoint 與 rollback 規則集中在 [deployment.md](deployment.md)。
目前只接受統一版 manifest，不執行舊 accepted replay。停止錯誤、查詢不明、遺失 checkpoint 都會阻塞，
不可把缺少觀察當成成功或不存在。

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
