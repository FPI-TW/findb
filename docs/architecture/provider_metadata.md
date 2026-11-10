# Provider metadata

`backend/configs/provider_catalog.v1.json` 定義本次統一的四個 providers／五條 feeds：
Twelve Data US EOD、FactSet HK EOD、FinLab TW equity／ETF EOD、TAIFEX futures EOD。
Shioaji 已完成退役，不列入這份 catalog；Twelve Data 不提供 HK。

## 目前導入階段

PR1 已完成 [staging 產物與服務驗收](../operations/deployment.md#provider-catalog-pr1-staging-驗收)。
PR2 將 Backend Source scopes、credential specs、feed descriptors、calendar 設定名稱、historical
capability 與 active provider 型別接到生成 metadata，並新增唯讀 execution-config API。
`app.services.provider_metadata` 在 process 內載入並驗證 image 隨附的 metadata digest；
發布新版 Backend 後使用新值，不提供環境排程 override。

Fetcher daily 每輪規劃前讀取 Source execution-config，image 僅保存非排程 execution descriptors 與
bootstrap。Dashboard 依 market 分組，逐 feed 顯示時間與時區，不顯示 Slot。DB schedule 是 catalog
投影；新 plan 與無 plan 的監控使用 catalog deadline，已有 plan 使用 frozen `deadline_at`。
Historical runner／planner 不讀 daily 排程 API，也不再依賴本機排程檔。
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
- Schedule 定義交易所當地時間與 date lag；completion deadline 使用獨立 timezone／trade-date day offset。
  Full daily 保留 US 次日台北 09:00、其餘當日台北 23:00。Bounded monitor 的 US 次日 09:15、
  TW equity 當日 17:00 另行保存；其它目前停用。Publication retry window 不代表 delivery SLA。
- Backend 的 market→時區只由 `app.services.effective_calendar.MARKETS` 維護，包含 `TAIFEX` 這類
  calendar market；`market_timezone()` 對未知 market fail closed，不回落台北時間。Catalog feed 的
  `market`／`calendar_market` 都必須在其中，`schedule.timezone` 必須與之相同（`test_provider_catalog.py`）。
  Full-market admission／acceptance、historical provider cutoff、FactSet 窗口、HK cutover 與 seed
  delivery expectation 都引用這份對照。
- `delivery.slot_id` 只表示既有 contract 相容欄位與 Dashboard 區域分組，不能作為時鐘或新 acquisition
  權威。Seed 的 delivery expectation 與 `scheduler_control` 的 slot、時鐘、時區都由 catalog 投影
  （`seed_datasets` 會更新現役 control 的 slot），既有列由 migration `c3f6a9d1e2b7` 投影一次；
  `test_catalog_slot_migration.py` 強制 seed、frozen migration 與 catalog 一致。
- Historical execution profiles 保留現行 local／staging／production 的靜態 universe reference 與 executor
  預設值，不包含觸發時間。這些是打包預設，不是 DB request、operator overrides 或即時授權。

Catalog 不存 secrets、account digest／identity、usage、cooldown、Owner controls、HK cutover state 或
published calendar。Adapter dispatch、SDK 行為、credential 正規化留在原模組。

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
| `infra/deploy/generated/provider_inventory.v1.json` | 三 images、四 providers、八 consumers 的明確 inventory |

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
| Fetcher | Full-market／historical／backfill planner／account governor CLI 的 provider choices、Source authorization 回應的 provider 檢查、平台 rate／response bounds、單一 credential 設定名稱、readiness evidence 的 response bound | `execution_descriptors.v1.json` |
| Dashboard | Provider options 與 scope | `provider-catalog.ts` |
| Infra | Consumer 名稱、provider↔image key、需驗證與拉取的 image 集合、installation inspector provider choices | `provider_inventory.v1.json` 與 guard 生成區塊 |

刻意保留手寫、且不放入 catalog 的項目：

- **Adapter 語意集合**：`historical_backfill.DAILY_REPORT_HISTORICAL_PROVIDERS`（TAIFEX／FactSet
  讀官方或 vendor 日報，休市日一律略過，cutoff 含 provider 當日）與 `PREVIEW_CALENDAR_PROVIDERS`
  （FactSet preview 不套用 calendar switch；保留退役 Shioaji 舊 request preview）。這些是 executor
  行為而非 provider 靜態能力，Catalog 的 `declared`／`executable` 沒有定義此語意；測試確認成員仍是
  catalog historical providers，且 daily-report providers 沒有 Fetcher universe descriptor。
  Fetcher `historical_backfill.py` 的 partial／manifest 結果判斷與 `production_backfill_cli` 的請求估算
  單位同屬 adapter 行為，保留原處。
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
calendar snapshot、cutoff 與 acquisition authorization 保留。新建 Twelve Data／FinLab historical
payload 的 `delivery.scheduled_for` 使用 request 交易日 UTC 00:00 作為穩定日期標記，不表示 daily
觸發時間；已有 prepared body 完全沿用原值。Slot 只供現行交付契約與 journal identity 相容。

Planner 以相同 descriptor／reviewed universe 估算，Backend preview 決定日期及權限是否合法；
FinLab ETF 等 unsupported historical scope 不因日資料能力而開放。詳細操作見
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
