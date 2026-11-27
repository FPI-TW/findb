# Provider metadata

`backend/configs/provider_catalog.v1.json` 定義六個 providers／十三條 feeds：
Twelve Data US EOD、FactSet HK EOD、HKEX 官方每日行情（`hk_equity_official_eod`，契約 `exchange_daily_quote.v1`）、
FMP 市場指數日線（`hk_index_eod`、`us_index_eod` 與 `us_dollar_index_eod`，契約 `market_eod.v2`，`asset_class` 為 `index`；
同一市場可有多個指數 dataset，Serve 依市場與資產類別讀取，`/datasets` 以各 dataset 自己寫入的資料計算涵蓋範圍）、
FinLab TW equity／ETF EOD、TAIFEX futures EOD，以及 FinLab 的四條盤中 feed
（`tw_equity_bar_1m`、`tw_etf_bar_1m`、`tw_equity_tick`、`tw_etf_tick`，契約 `intraday_partition.v1`）。
只有盤中 feed 的頻率不是 `daily`：1 分 K 為 `minute`，逐筆為 `tick`。
Shioaji 已完成退役，不列入這份 catalog；Twelve Data 不提供 HK。

## 目前導入階段

PR1 已完成 [staging 產物與服務驗收](../operations/deployment.md#provider-catalog-pr1-staging-驗收)。
PR2 將 Backend Source scopes、credential specs、feed descriptors、calendar 設定名稱、historical
capability 與 active provider 型別接到生成 metadata，並新增唯讀 execution-config API。
`app.services.provider_metadata` 在 process 內載入並驗證 image 隨附的 metadata digest；
發布新版 Backend 後使用新值，不提供環境排程 override。

Fetcher daily 每輪規劃前讀取 Source execution-config，image 僅保存非排程 execution descriptors 與
bootstrap。Dashboard 依 market 分組，逐 feed 顯示市場、時間、時區、抓當日或前幾日的資料與下次執行時間，不顯示 Slot；
一個 control 的 feeds 涵蓋多個市場時（FMP 指數），卡片只出現一次，放在合併的市場分組（例如 `HK / US`）。DB schedule 是 catalog
投影；新 plan 與無 plan 的監控使用 catalog deadline，已有 plan 使用 frozen `deadline_at`。
Historical runner／planner 不讀 daily 排程 API，也不再依賴本機排程檔。
兩環境排程 DB／API／UI 的核對見 [PR5 兩環境整合驗收](../operations/deployment.md#pr5-兩環境整合驗收)。
未完成範圍見 [backlog](../dev/backlog.md#provider-metadata-與排程統一)。

## 宣告與邊界

- Provider 定義穩定 ID、container suffix、image 共用、consumer roles、credential **設定名稱**、
  Source API rate limit 與平台靜態限制。TAIFEX 共用 Twelve Data image，兩者仍是不同 provider。
- Feed 以 `(provider, dataset_key)` 識別；Source、acquisition、historical 能力分開。
  FinLab ETF 支援日資料，但目前不支援 historical，不因 ingress scope 自動擴張。
- Lifecycle 分為 active／draining／retired。Acquisition 與 historical 只允許 active Source feeds；
  consumers 由明確 roles 定義，不能因沒有 acquisition 就自動刪除排空程序。
- Contract 明列 current／accepted versions，並核對 `contracts/manifest.json` 與實際 artifact checksum。
  FactSet 新交付為 market_eod v2，accepted 為 v1／v2。
- Schedule 定義交易所當地時間與 date lag；completion deadline 保留自己的 timezone／trade-date day offset 欄位，
  但值必須是排程開始後 12 小時、與排程同時區（`COMPLETION_DEADLINE_HOURS_AFTER_SCHEDULE`），catalog 載入時
  檢查。Fetcher 的 `no_data` 最後一輪重試依這個期限排定。Bounded monitor 的 US 次日 09:15、
  TW equity 當日 17:00 另行保存；其它目前停用。Publication retry window 不代表 delivery SLA。
- Backend 的 market→時區只由 `app.services.effective_calendar.MARKETS` 維護，包含 `TAIFEX` 這類
  calendar market；`market_timezone()` 對未知 market fail closed，不回落台北時間。Catalog feed 的
  `market`／`calendar_market` 都必須在其中，`schedule.timezone` 必須與之相同（`test_provider_catalog.py`）。
  Full-market admission／acceptance、historical provider cutoff 與 seed delivery expectation 都引用這份對照。
- `delivery.slot_id` 只表示既有 contract 相容欄位與 Dashboard 區域分組，不能作為時鐘或新 acquisition
  權威。Seed 的 delivery expectation 與 `scheduler_control` 的 slot、時鐘、時區都由 catalog 投影
  （`seed_datasets` 會更新現役 control 的 slot），既有列由 migration `c3f6a9d1e2b7` 投影一次；
  `test_cross_timezone_schedule.py` 強制 seed 與 catalog 一致。
- `historical_cohort` 宣告 historical 日期如何凍結成員，historical feed 必填、其它 feed 為 null：
  `dated_release`（每個日期綁定當天適用的 Full release，早於第一份 release 的日期沿用最早那份，並補查之後
  清單多出的成員；Twelve Data、FinLab）、`current_release`
  （整個 request 綁定建立時的現行 release；FactSet 只能查當下名單）、`daily_report`（成員來自當天報表，
  無凍結名單；TAIFEX）。Historical 沒有打包的靜態 universe。Backend 的
  cohort 綁定、成員驗證與 EOD completion 依此欄位，不再依 provider 名稱；`current_release` 的 frozen
  snapshot 仍寫 `cohort: current_hkex`，這是 Fetcher executor 檢查的既有 wire 值。兩種 release cohort 的
  historical item 以成員證據完成，`daily_report` 以當日報表證據完成，沒有以單一 Source run 完成的路徑；
  Admin preview 依同一欄位回傳每個日期將凍結的成員數，供 planner 估算。
  兩種 release cohort 的成員證據都可以把「沒有可引用回應」的成員（provider 沒有該檔序列，或每次嘗試都失敗、
  重試用完）記為 `missing` gap，該日期以 `partial` 結算，不會卡住同一 provider 之後的 historical 工作；
  daily plan 的成員不適用（Owner 於 2026-11-24 決定）。Mapping gap，以及 `current_release` 裡交易日當天
  名單尚未列入的成員是 unavailable，不讓日期 partial（#593）。
- `historical_requests_per_date` 是 planner 估算每個日期的 provider 請求數：固定次數（共用報表或表格，例如 TAIFEX
  五個商品、FinLab 五張表、FactSet 一次查詢），或 `per_member`（每個成員一次；Twelve Data），historical feed 必填。
  `per_member` 只能用在 `dated_release`，因為要有成員清單才能計數。
- `historical_lookback_days` 可延長該 feed 在 production 的回補窗口（367–3660 個 calendar days），`null`
  沿用預設的最近 366 天；只有 historical feed 可以設定，local／staging 一律維持 31 天。建立、preview、claim
  與過期判定都依 feed 計算這個邊界。
- `universe_mode` 宣告交易日名單的來源：`refreshed` 由 maintenance 刷新清單，plan 凍結最新 published
  release；`dated` 由當天的 provider 觀測建立（TAIFEX 當天報表），plan 必須有該交易日的 release，
  否則 Source 以 `taifex_dated_universe_required` 回絕（既有 wire 值，Fetcher 收到後提交當天名單）。
  `dated` feed 不在 Owner 手動刷新清單、Fetcher 不為它排 maintenance，期貨換倉在有界的前進範圍內可超過
  一般 2% 變動門檻自動發布，缺當天名單的告警附 `DATED_UNIVERSE_PENDING` 細節。Backend 與 Fetcher 都只讀這個
  欄位；Fetcher 經 `execution_descriptors.v1.json` 取得，adapter 只提供 `dated_universe()` 實作。
- `unlisted_symbols` 宣告 Fetcher 是否也送出 provider 有提供、但不在凍結清單上的標的：`deliver` 或 `none`。
  清單只用來確認缺漏，不阻擋有資料的標的（#629）；只有回應涵蓋全市場的 provider 才看得到清單外的標的，
  所以只有 `refreshed` 的 `market_eod` feed 可以設 `deliver`（目前是 FinLab 台股與 ETF 日 K）。`dated`
  名單本身就是當天報表，逐筆請求的 provider 不會收到清單外的列，盤中 partition 也沒有列層級的代碼，
  這些都是 `none`，Source 對清單外的列照樣回 409。
- `calendar_required` 宣告 feed 的日期是否只能用已發布的交易所日曆。`true` 時缺日曆的日期不以 weekday
  預設代替，而是以 `CALENDAR_UNPUBLISHED` 等待重試，歷史回補也拒絕含未發布日期的範圍，見
  [Full market 維運](../operations/full_market.md)。目前只有兩條 FMP 指數 feed 是 `true`。
  只有 Backend 讀這個欄位；Fetcher 只依 Source 回的錯誤碼等待，不在 execution descriptors 裡。
- 同一 dataset 的 feeds 必須共用 `calendar_market`（loader 檢查）。Backend 以 dataset 查日曆市場時一律讀
  catalog，沒有 catalog feed 的退役 Shioaji 分 K dataset 沿用自身 market。

Catalog 不存 secrets、account digest／identity、usage、cooldown、Owner controls 或 published calendar。Adapter dispatch、SDK 行為、credential 正規化留在原模組。

## 生成與檢查

```bash
pnpm contracts:export
pnpm contracts:check
# 只處理 provider metadata
uv --directory backend run python scripts/export_provider_catalog.py
uv --directory backend run python scripts/export_provider_catalog.py --check
```

`app.provider_catalog` 使用 strict Pydantic models，拒絕額外欄位、無效 reference、重複 identity、
未知 contract、非法 lifecycle／capability、錯誤時區與時間。JSON Schema 是結構契約；跨欄位與
跨 artifact 的語意驗證由 loader 執行，不能只跑 JSON Schema 就宣稱通過完整檢查。

| 生成產物 | 用途 |
| --- | --- |
| `backend/configs/provider_catalog.v1.schema.json` | Catalog 結構 schema |
| `backend/app/generated/provider_metadata.v1.json` | Backend metadata、時間與 Source scopes |
| `backend/app/generated/provider_ids.py` | Active Source provider 型別；舊 Shioaji readiness evidence 額外保留明確歷史型別 |
| `fetcher/configs/bootstrap.{environment}.v1.json` | 程序版本、環境身分、polling；無 feed、觸發時間或 deadline |
| `fetcher/src/findb_fetcher/data/execution_descriptors.v1.json` | Fetcher 非排程 metadata，隨 Python package／image 打包 |
| `dashboard/src/lib/generated/provider-catalog.ts` | Typed provider constants 與 scope |
| `infra/deploy/generated/provider_inventory.v1.json` | 三 images、五 providers、十 consumers 的明確 inventory（HKEX 與 TAIFEX 共用 Twelve Data image） |

資料產物包含 schema version 與 SHA-256；Backend 保存 canonical source digest，其它消費端保存
各自正向欄位投影的 digest。來源物件與集合排序不改變輸出；只改 Backend 時間不改非排程 Fetcher
descriptor、Dashboard constants 或 Infra inventory。單純時間變更只發布 Backend，無須重建 Fetcher image。
獨立傳送至 SSM 的 host／recovery／owner-lock guards、installation inspector
（`inspect_full_market_installation.py`）與 `infra/monitoring/publish_staging_metrics.py`
內嵌同一生成 inventory，生成區塊納入 `--check`；OpenTofu monitoring 直接讀 inventory JSON 推導告警。
`--check` 不寫檔，任一產物缺失或內容被修改就失敗，已接入 FinDB／Fetcher CI。
Fetcher 使用正向欄位投影，不包含 schedule、deadline、date lag 或 publication retry window，避免
Backend 未來新增欄位時把排程設定一起發布到 Fetcher image。

新增／替換 feed 須先更新 catalog、確認 contract 已發布，重新生成並執行測試。消費端不可手改產物。
修改同語意欄位應只修改 catalog；舊 schedule manifest 僅作為 tests/fixtures 的相容性證據，不打包至 image。

## 衍生值與刻意手寫的邊界

下列 provider 資訊只由 catalog 經生成產物取得，消費端不另列 provider 名單：

| 消費端 | 衍生內容 | 來源 |
| --- | --- | --- |
| Backend | Source scope、credential specs、historical `CAPABILITIES`、calendar 設定名稱、Full-market feeds、cutover／owner-lock providers、readiness provider 型別、delivery plan `execution_policies` 的 provider 集合 | `provider_metadata.v1.json` |
| Backend seed | Catalog feed 的 provider（`allowed_sources`、`source_precedence`、expected source）、market、asset class、frequency、calendar market、contract schema 與 current／accepted versions（含 FactSet HK v2） | `provider_metadata.v1.json` |
| Fetcher | Universe 模式、Full-market／historical／backfill planner／account governor CLI 的 provider choices、Source authorization 回應的 provider 檢查、平台 rate／response bounds、單一 credential 設定名稱、readiness evidence 的 response bound | `execution_descriptors.v1.json` |
| Dashboard | Provider options 與 scope | `provider-catalog.ts` |
| Infra | Consumer 名稱、provider↔image key、需驗證與拉取的 image 集合、installation inspector provider choices | `provider_inventory.v1.json` 與 guard 生成區塊 |

刻意保留手寫、且不放入 catalog 的項目：

- **Universe 檔格式與 executor 重試**：`production_backfill_cli` 依 provider 選擇 reviewed universe 檔的解析器
  （Twelve Data 與 FinLab 的檔案格式不同）；historical runner 以 `retries_operations` 宣告自己在 lease 內重試每個
  操作（TAIFEX），此時 Source 授權每次只送一次、暫時拒絕交回 runner 重試，其它 runner 使用 Source client 的重試。
  兩者都是 executor 實作，不是 feed 規則。
- **Credential 正規化**：FinLab SDK token、FactSet `HOST:PORT:USER`（不含密碼，輪替保留帳務）為
  adapter identity；名稱須是 catalog 已發布的 credential 設定（測試約束）。多於一個設定名稱且未定義
  adapter 規則的 provider fail closed。
- **Host durable 目錄**：`deploy_fetcher_aws.sh` 的 `provider_host_layout()` 保存既有 historical state 與
  FinLab SDK cache 路徑（Twelve Data 沿用舊 `/var/lib/findb-fetcher`）。這是既有磁碟狀態位置，修改 catalog
  不得搬移 state；inventory 出現沒有 host layout 的 provider 會在任何 Docker 操作前以
  `provider_inventory_invalid` 失敗。
- **Image reference 環境變數**：`TWELVE_IMAGE_REF`／`FINLAB_IMAGE_REF`／`FACTSET_IMAGE_REF` 是 workflow、
  release manifest 與 deployment transaction 的介面；腳本只保留 image key→變數的單一對照，未知 image key
  fail closed。改名須與 workflow 同步發布。
- **Seed-only 欄位**：名稱、說明、currency、delivery expectation 的 mode／門檻（slot、時鐘、時區與
  lag 由 catalog 投影）、`full_market` gate、staged datasets 的 `is_active` 由 operator 管理，以及無 catalog feed
  的保留分 K datasets。`SUPPORTED_DATASETS` 是 registry provisioning 的獨立預期，不由 seed 自身推導。

## Backend 排程發布

`GET /api/v1/source/execution-config?execution_profile=full_market` 必須使用 Source credential；
可選 `schema_version=1`，其它版本、profile（含已退役的 `pilot`，回傳 422）、provider／environment 查詢參數均拒絕。
Provider 與可見 feeds 由 credential 決定；NULL、空、重複、越權或無完整排程的 scope 不發布部分結果。
回應含 `schema_version`、`environment`、`execution_profile`、`provider`、`schedule_digest` 與完整 `feeds`，
並設定 `Cache-Control: no-store`。Digest 為移除 `schedule_digest` 後的 JSON，以 sorted keys、
compact separators、ASCII escape 編碼再做 SHA-256；排序後的 feed 陣列及環境／profile 都參與摘要。

每條 feed 提供 identity、contract、`scheduled_time`、`source_timezone`、`target_date_lag_days`、
`publication_retry_window_seconds`、獨立 `completion_deadline` 及 `bounded_monitor`。
`publication_retry_window_seconds` 不在 catalog 設定，由完成期限推算（排程後 12 小時減 30 分鐘，41,400 秒），
所以尚未發布的日期一路重試到期限前最後一輪；catalog 若宣告這個欄位即載入失敗。
`source_timezone` 可以和交易所時區不同：目標日期是排程時區當天往前 `target_date_lag_days` 天的交易所日期，
收盤、日曆與休市判斷仍依交易所時區，完成期限依排程時區與 lag 計算（例如台北 08:30 抓前一個紐約交易日，
期限為台北當天 20:30）。`asset_class` 可為 `equity`、`etf`、`future` 或 `index`；指數名單成員的代號沿用
provider 的寫法，只有 `index` 可以用 `^` 開頭（例如 `^GSPC`）。
不包含 slot；發布窗口不是 deadline。此 endpoint 不檢查 running、universe readiness、
quota 或 historical lease，也不建立 plan／work；取得設定不代表取得 acquisition authorization。

`app.services.feed_schedule` 統一 Source 與 Admin 的時間解讀。Admin `/schedulers` 新增
`feed_schedules`，`/market-freshness` 的每條 feed 新增 `schedule`，皆附 `next_scheduled_at`。
該值是下一次嚴格晚於評估時間的當地時鐘觸發，隨紐約 DST 換算 UTC；不是交易日日曆判定。
Retired／非 acquisition feeds 的 schedule 為空。Card 下一次時間取逐 feed 的最早值。
已有 plan 的期限優先於新 catalog，不改寫歷史 evidence。

Bounded missing-delivery 使用獨立 `deadline_timezone`／`deadline_day_offset`，US 仍是次日
台北 09:15，不使用 daily trigger 的 timezone／date lag。Ingest close-plus-grace 不變。
遷移內嵌固定目標與已核對舊值，未知差異中止；seed 保留 enabled、action、threshold、notes，
重新投影排程與期限。操作見 [PR5 發布與遷移](../operations/deployment.md#provider-catalog-pr5-發布與遷移)。

## Historical 與 daily 隔離

PR3 的 `findb_fetcher.execution_metadata` 只載入 package 內的 `execution_descriptors.v1.json`，
驗證版本、digest、唯一 identity、環境與 historical capability。Twelve Data／FinLab 使用不含
觸發時間的 `ExecutionDescriptor`；FactSet／TAIFEX 從相同投影讀取交付 metadata。
既有 scheduler service 會將非排程參數轉為 descriptor，並另外提供 daily delivery metadata，
共用 executor 本身不計算觸發時間。

Historical 日期仍來自 request／lease；不讀 Source execution-config、不檢查 daily running，亦不因
daily 時間變更改變回補範圍。既有 journal identity、attempt、universe freeze、prepared bytes、
calendar snapshot、cutoff 與 acquisition authorization 保留。本變更前建立的 Twelve Data／FinLab
historical request 沿用舊 executor，payload 的 `delivery.scheduled_for` 使用 request 交易日 UTC 00:00
作為穩定日期標記，不表示 daily 觸發時間；已有 prepared body 完全沿用原值。新 request 凍結各日期的
Full release cohort，成員範圍由 Backend 決定，不讀 descriptor 的 universe。Slot 只供現行交付契約與
journal identity 相容。

Planner 以相同 descriptor／reviewed universe 估算，Backend preview 決定日期及權限是否合法；
historical capability 只由 catalog 宣告開放（FinLab `tw_etf_eod` 已為 `executable`），不因日資料能力
自動開放。詳細操作見
[historical runbook](../operations/data_maintenance.md#production-universe與historical-backfill)。

## Fetcher 動態排程

`findb_fetcher.execution_config` 驗證 schema、environment、profile、digest、完整 provider scope、
已安裝 adapter／contract、時區、clock、lag 與窗口。Source client 每輪只讀取一次有上限的
`no-store` 回應，完整驗證後才建立記憶體快照。多 feed 在同輪共享版本；下一輪重新讀取，
失敗先清除前輪快照並停止新日期規劃，不存在磁碟 cache、image fallback 或環境時間 override。
`FETCHER_BOOTSTRAP_CONFIG` 只綁定安裝憑證的 bootstrap identity；舊 full-market config 不再被接受。

新 daily work 將該輪 feed metadata 凍結在既有 `scope.feed`。後續排程更新不改 stable key、
既有 work 或 plan 的 `deadline_at`。重啟遇到同一 daily identity 的舊 feed，Source 回傳
`409 / PROVIDER_WORK_FROZEN_FEED`；Fetcher 只略過該次重複 enqueue，claim 後仍用原 scope。
Provider／dataset／date／profile 或 adapter identity 真正衝突仍失敗，calendar snapshot 亦不重寫。Slot 由非排程 descriptor 加入既有交付 metadata，
不參與 API 快照驗證、到期日、deadline 或排程握手。

Universe 日期解讀與 `source_timezone` 一律使用 Source 發布的 feed 時區，Fetcher 不保存
market→時區對照：cycle 內沿用當輪快照；未持有快照的 maintenance（含 drain 與手動同步）在任何
provider I/O 前另讀一次 execution-config。Maintenance 只依 credential 綁定授權，因此只要求本次 feeds
已發布，不要求完整 provider scope，其餘身分、digest 與 feed 驗證不變；失敗或缺少該 feed 即不抓取、不提交。TAIFEX dated
universe 使用 frozen `scope.feed` 的時區。只讀時區，不引用 daily trigger 時間，也不對
historical 增加 daily API 前提。

每輪先透過原 owner／Source authorization 排空已入列工作，再規劃新日期；drain 不以排程規劃新日期，
不 enqueue 新日期或 maintenance。Source 不可用時原 prepared bytes／收據保留，待恢復後重送。
Historical request、lease、calendar snapshot、cutoff 與權限保持獨立。

## 發布邊界

PR4 沿用 schema-v4 單次部署交易，Fetcher bundle 格式升至 4，allowlist 改為 bootstrap 加生成 inventory。
已接受的 bundle 3 仍可被驗證與回復，但新 runtime 不讀舊 config。Backend owner-lock providers 由
同一 catalog 的 consumer roles 推導。部署與 production 分組見
[deployment](../operations/deployment.md#provider-catalog-pr4-發布邊界)。
