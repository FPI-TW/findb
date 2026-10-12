# Full market 啟動、平台配額與治理資料

`FULL_MARKET_ENABLED=false` 是 local、staging、production 的預設值。Backend 與 Fetcher 必須使用
相同 `APP_ENVIRONMENT=local|staging|production` 與旗標。既有 JSON `deployment_target` 仍是環境匹配契約；legacy replay/inspection 窄 bridge 見 deployment runbook。GitHub Environment 的 `FULL_MARKET_ENABLED` 決定一般 deploy 的
`bounded`／`full-market` profile；rollback 從 immutable accepted record 讀取原 profile，不修改 bundle。
旗標為 true 不會啟動排程。Full market 的啟動資料條件只包含已發布 baseline 與完整已發布 calendar。
Owner 權限、revision CAS 與 Source 認證屬於既有操作／資料存取規則。
Full API 的 Source credential 必須有明確、非空的 dataset list，且涵蓋 admitted feeds；
NULL、空值或 malformed scope 一律拒絕授權與 universe／plan 存取。
maintenance bootstrap 也必須通過有效 provider／dataset scope 與 credential 期限檢查，
但無須先建立 admission 或發布 baseline；授權回應的 blockers 僅反映 maintenance 條件，
並以 `authorized_dataset_keys` 限制官方名單刷新。Running cycle 刷新成功或失敗後，
universe、plan 與新資料抓取仍僅使用當次凍結的 admitted feeds。正常 credential create／rotate
會將省略的 scope 展開成 provider 的明確清單；本規則不放寬既有 API 邊界。

2026-10-12 staging 已明確 opt-in：`staging-findb`／`staging-fetcher` GitHub Environment 與兩組
本機 `.env.remote` 的 `FULL_MARKET_ENABLED` 均已同步為 `true`。同一 protected-main revision
`5ef7540e340331a3afd502efca75bcd7b5199c20` 依序完成
[FinDB CD](https://github.com/FPI-TW/findb/actions/runs/38194939788) 與
[Fetcher CD](https://github.com/FPI-TW/findb/actions/runs/38195805655)，包含各自 CI、candidate acceptance
與 activation。Fetcher accepted manifest 的 `runtime_profile=full-market`。

獨立 SSM commands `78381ba8-a11f-4542-ad3f-3b41f8938660`（FinDB）與
`281a02ec-0325-4af0-a108-864567688dd0`（Fetcher）均成功（exit 0），確認 FinDB
Serve／Ingest／Worker／Dispatcher／Raw cleanup、四個 Pilot scheduler 與四個 Full workers 的容器
環境皆為 `APP_ENVIRONMENT=staging`、`FULL_MARKET_ENABLED=true`，全部 running、restart count 0；
Fetcher 容器皆為 accepted。公開 `/health` 回傳 healthy。八個 scheduler 的 desired／observed state
皆為 stopped，且都有新 heartbeat；部署前後 control revisions 未變。Full readiness／account 文件
尚未安裝，部署版本仍有舊 enrollment gate；本分支移除該 gate，發布新版後只需 baseline／calendar 與 Owner 手動啟動。
此紀錄為查核當時的狀態；後續部署後須重新確認實際值。

## Staging readiness 查核（2026-10-12）

11:03（Asia/Taipei）以現行 Backend eligibility 查核四個 `full_market_*_v1` controls，
皆為 `start_allowed=false`、desired／observed stopped、revision 5 且 heartbeat fresh。
共同 start blockers 為 `full_market_runtime_missing`、`full_market_no_ready_feeds`；前者表示
DB 尚無 trusted enrollment，不代表 Docker worker 未安裝。七個 feeds 皆缺 published baseline，
DB 無 candidate 或 published universe。US／HK／TW／TAIFEX 2026 calendar 各有完整
published revision（365／365），現行 eligibility 未回報 calendar 或 registry configuration blocker。

已完成以下可獨立處理的設定：

- 以實際 installed Source key hashes 綁定既有四個 Source clients，執行 credential reconciler 的
  `full-market` profile。Twelve Data scope 包含 US／HK EOD；FinLab 包含 TW equity／ETF EOD；
  Shioaji 包含 TW equity／ETF minute；TAIFEX 為 TW futures EOD。四個 internal Source limits
  均為 2400 RPM，既有 client UUID／key hash 保留；calendar Serve credential unchanged，
  reconciler 寫入治理 audit。
- 首次建立 `/var/lib/findb-account/staging/governor.sqlite3`，owner 10001、mode 0600，
  integrity check 為 ok；quota／work／plan／capacity violation／repair tables 初始皆空。
  空 governor 不表示 provider 當日用量為零，SDK API 的帳號累計用量仍為平台配額權威。
- 從 immutable accepted S3 object 複製原始 acceptance record，連同 installed manifest／config、
  有上限的 quota probe plan 與去敏觀察結果保存至 Fetcher host 的
  `/etc/findb-full-market/evidence/staging/c035a87dda0f261142860b451ac645263982fc75d3a2ec8ff184f1e95216afb2/`。
  目錄 0700、檔案 0600；此處的 `readiness-constraints.json` 為 incomplete observation，
  不可當作 verified readiness／account declaration 安裝。

11:01（Asia/Taipei）的 bounded quota observation 結果：

| Provider | 已取得的額度／用量證據 | 尚缺的治理資料 |
| --- | --- | --- |
| Twelve Data | `/api_usage`：`plan_limit=1597`、`current_usage=1`，單位為 credits／minute；一次 probe 消耗 1 credit | US／HK official universe、provider symbol mapping、Owner published baseline |
| FinLab | SDK 1.5.7 quota metadata：`limit_size=5000`、`quota=33.292991638183594` MB；未下載 table | TW equity／ETF official universe、provider symbol mapping、Owner published baseline |
| Shioaji | SDK 1.7.1 simulation `usage()`：`bytes=0`、`limit_bytes=remaining_bytes=524288000`（500 MiB）；50 calls／10 秒 | TW equity／ETF official universe、catalogue mapping、Owner published baseline |
| TAIFEX | 公開 OpenAPI，不需 provider 帳號；scoped Source client 與 worker 已安裝 | 實際月／週合約與日盤／盤後 official baseline、Owner publish |

七個 feeds 尚無 published baseline；具體交付資料見
下方「治理資料明確需求」。此查核記錄來自當時 staging 的舊部署；本工作分支已將啟動資格
與新抓取均只依 baseline／calendar 及既有操作控制、API 認證；本分支尚未部署。

驗證 SSM commands：Source 校準 `707a9976-8a48-4ef4-9416-bb9bad8e535d`、quota observation
`f0bb370e-cc21-4f91-9e0e-ebc2a5c6fcf7`、Backend 最終 eligibility
`71617f8e-f60f-451e-8408-4439dc70dd8d`、Fetcher 最終容器／governor
`0fd99533-bedf-495d-9d7a-bebad9f23d03` 均為 Success／exit 0。八個 Fetcher containers
均 running、flag true、restart count 0。

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
及 provider API 認證與資料驗證。此分支尚未部署；live staging 的既有查核不能代表新版已生效。

## 治理資料明確需求

啟動必要治理資料只包含以下兩類，其餘診斷／部署資料不列為啟動前提：

| 資料 | 必備內容 | 驗收方式／目前 staging 狀態 |
| --- | --- | --- |
| Published baseline | 每 provider／dataset 的 release ID、effective date、完整官方成員／provider mapping、來源 URL／SHA256 與不可變原始資料參考、member count／SHA256、Owner publication timestamp／audit | 同 provider／dataset、status=`published`、effective date 不晚於當地今天；七個 feeds 目前皆缺。首份發布提供 `evidence_note` 與 `first_baseline_approved=true`；重大成員異動仍遵循既有 publish 規則 |
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

Local 範例（正常 Source、provider、calendar 與 R2 env 須已設定）：

```bash
export APP_ENVIRONMENT=local FULL_MARKET_ENABLED=true FETCHER_CONSUMER_PROFILE=full_market
export FETCHER_ACCOUNT_STATE_PATH=/private/tmp/findb-local-account/governor.sqlite3
fetcher/.venv/bin/findb-fetch-full-market --provider finlab \
  --config "$PWD/fetcher/configs/full_market.local.v1.json" \
  --state-path /private/tmp/findb-local-full/state.sqlite3 --sync-universes
fetcher/.venv/bin/findb-fetch-full-market --provider finlab \
  --config "$PWD/fetcher/configs/full_market.local.v1.json" \
  --state-path /private/tmp/findb-local-full/state.sqlite3 --run-forever
```

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
既有 debt/cooldown 不重設，也不輸出 credential。Twelve Data 與 FinLab 的 fingerprint
使用 provider config 去除前後空白後的有效 credential；同一有效 credential 的空白格式變動
不另建 ledger。Full Shioaji 使用 persistent session 實際傳入 SDK 的原值，不套用額外正規化。
首次升級尚未建立對照時，若舊 allocation 遺失、損壞或沒有可用 account ID，舊 schema 沒有
provider／credential 關聯可供還原。此例外以 credential fingerprint 建立新 ledger，只依平台
實際配額；原 ledger 用量與 cooldown 保留供稽核，不猜測、改標籤或合併匿名債務。
此操作接受舊用量與 cooldown 無法接續的風險；應在首次升級前保留可辨識的 allocation 與
governor 以維持完整接續。不同環境不設分配或預算，平台累計用量與服務端限流仍包含外部用量。
Governor 保留用量、pacing、cooldown；舊 capacity violation/repair 資料保留稽核，正常抓取不再
以它們作前提，也不刪除既有 debt。HTTP／SDK／IPC／R2 大小、格式與 provenance 驗證仍保留；
過大或不合法 response 不得變成成功 payload，但不新增永久 account capacity 封鎖。

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
再進入 immutable coordinator 與原本已驗證的 legacy environment bridge。因此 pre-marker accepted bundle
同樣受保護，仍使用其記錄的 profile，manifest／bundle 內容不改寫。既有 accepted bundle／image 可正常 replay，
checkpoint 遺失或不合法時需 operator 處理 durable state，部署不會重建未知 prepared work。
目前 transported 離線測試使用 frozen coordinator／helper／CLI excerpts 與 no-op registration，證明
拒絕順序及舊 bootstrap 邊界，未完整覆蓋舊 coordinator 的 flags／traps／pointer 與成功 replay。
另有完整 HEAD coordinator 的 unknown Cmd proof，確認 current guard 拒絕時零 effects、IDs／pointer 保留；
此證據不等同完整 accepted bundle 成功 replay。完整離線 harness 見 backlog，實際成功 replay 仍屬外部驗收。
Docker `--check`／`--require-stopped` probes 可使用 `cache_dir=-`，空 cache mount 在 Bash 3.2 下同樣安全。
`--require-stopped` 僅以 read-only SQLite 驗證舊 compatible checkpoint 也具備的 core tables，再查詢
Source desired state；缺檔／不合法 state 會在載入 credential 與 Source 呼叫前拒絕，既有 prepared
bodies、debt、cursor 不改寫。只有沒有既有 Full worker 的真正首次 Full 安裝可執行明確 initializer。

所有 HTTP status 都先做 bounded read 與 byte/witness 記帳，包括 Twelve Data daily、官方 universe、TAIFEX fallback
report 的非 200 回應與中途 transport failure；error body 不解析為 provider data/universe。
已知 429 在讀取 body 前就將 60 秒 cooldown 持久化至 active Permit 的實際 shared account/governor，
即使 body 超限、encoding／transport／observer 失敗也保留 cooldown。Twelve Data 的 Pilot／historical
client 使用自己的 shared Permit，即使未提供 Full callback 也遵守同一帳號 cooldown。所有非 200
（包含成功形狀的 201／206）一律拒絕；錯誤 JSON 只供遮蔽 secrets 的診斷，不成為成功 payload。
中途 transport error 保留實際已讀 bytes，不能把未接收 body 當 debt。其他 consumers 與重啟程序遵守同一帳號 cooldown；
普通 bounded runtime 保留原 provider 行為；實際 SDK 用量與 cooldown 不退款；prepared Source drain 不受影響。
已知超限 Content-Length witness 先於 unsupported encoding 拒絕保存；buffered 非 identity content
可能已解碼，其 decoded 長度不作為 actual wire debt；只有可取得的 raw-byte counter 才計 actual，
無可觀測 wire counter 時保留未知為 0 與 持久化用量。

舊 production Full runtime 曾使用 Pilot 的 `findb-fetcher-*-scheduler` names。升級或 flag-off bounded
部署會先 graceful stop，嚴格驗證非 OOM、無 Docker error 與合法 retirement exit，再搬至獨立 Full
names；既有 accepted image/identity/checkpoint 不刪除。每次 stop/rename 前登記交易 recovery entry，
失敗或 INT/TERM/HUP 時先倒序恢復 provider，再恢復原 names 與原 running/stopped 狀態。若有 accepted `-previous` 殘留，先停止／嚴格驗證並搬至獨立 Full `-legacy-previous` backup，避免 Pilot 復原誤選舊容器；失敗先恢復 current original，再恢復舊 previous，成功才清除這個非 current backup。退役失敗
會中止交易，不能透過強制刪容器跳過。Owner 應先停止兩個 profiles；idle `--run-forever` process
仍 running 是受支援的升級情境，deploy helper 負責優雅停止。
