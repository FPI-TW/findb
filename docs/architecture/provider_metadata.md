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

Fetcher 仍讀本機設定；`execution_feeds.v1.json` 與三環境 full-market configs 現為 catalog 的
過渡生成投影，不能手改。Dashboard 增加可讀取的逐 feed API 欄位，但 UI、DB schedule、
monitor 判定與 plan deadline 切換仍由後續 PR 處理。Historical runner 尚未解除舊檔依賴，
不能先刪除相容檔案。未完成範圍見 [backlog](../dev/backlog.md#provider-metadata-與排程統一)。

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
  Pilot／Full 保留 US 次日台北 09:00、其餘當日台北 23:00。Bounded monitor 的 US 次日 09:15、
  TW equity 當日 17:00 另行保存；其它目前停用。Publication retry window 不代表 delivery SLA。
- `delivery.slot_id` 只表示既有 contract 相容欄位，不能作為時鐘或新 acquisition 權威。
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
| `backend/configs/execution_feeds.v1.json` | 舊工具的相容投影，Backend acquisition metadata 不再讀取此檔 |
| `fetcher/src/findb_fetcher/data/execution_descriptors.v1.json` | Fetcher 非排程 metadata，隨 Python package／image 打包 |
| `dashboard/src/lib/generated/provider-catalog.ts` | Typed provider constants 與 scope |
| `infra/deploy/generated/provider_inventory.v1.json` | 三 images、四 providers、十二 consumers 的明確 inventory |

資料產物包含 schema version 與 SHA-256；Backend 保存 canonical source digest，其它消費端保存
各自正向欄位投影的 digest。來源物件與集合排序不改變輸出；只改 Backend 時間不改非排程 Fetcher
descriptor、Dashboard constants 或 Infra inventory。過渡期間的舊 full-market configs 仍含時間，
因此仍會改變；PR4 移除這些檔案後，時間調整才不再要求 Fetcher image 改版。
`--check` 不寫檔，任一產物缺失或內容被修改就失敗，已接入 FinDB／Fetcher CI。
Fetcher 使用正向欄位投影，不包含 schedule、deadline、date lag 或 publication retry window，避免
Backend 未來新增欄位時把排程設定一起發布到 Fetcher image。

新增／替換 feed 須先更新 catalog、確認 contract 已發布，重新生成並執行測試。消費端不可手改產物。
修改同語意欄位應只修改 catalog；Fetcher 舊 manifest 由 exporter 使用同一來源產生。

## Backend 排程發布

`GET /api/v1/source/execution-config?execution_profile=pilot|full_market` 必須使用 Source credential；
可選 `schema_version=1`，其它版本、profile、provider／environment 查詢參數均拒絕。
Provider 與可見 feeds 由 credential 決定；NULL、空、重複、越權或無完整排程的 scope 不發布部分結果。
回應含 `schema_version`、`environment`、`execution_profile`、`provider`、`schedule_digest` 與完整 `feeds`，
並設定 `Cache-Control: no-store`。Digest 為移除 `schedule_digest` 後的 JSON，以 sorted keys、
compact separators、ASCII escape 編碼再做 SHA-256；排序後的 feed 陣列及環境／profile 都參與摘要。

每條 feed 提供 identity、contract、`scheduled_time`、`source_timezone`、`target_date_lag_days`、
`publication_retry_window_seconds`、獨立 `completion_deadline` 及 `bounded_monitor`。
不包含 slot；發布窗口不是 deadline。此 endpoint 不檢查 running、FULL flag、universe readiness、
quota 或 historical lease，也不建立 plan／work；取得設定不代表取得 acquisition authorization。

`app.services.feed_schedule` 統一 Source 與 Admin 的時間解讀。Admin `/schedulers` 新增
`feed_schedules`，`/market-freshness` 的每條 feed 新增 `schedule`，皆附 `next_scheduled_at`。
該值是下一次嚴格晚於評估時間的當地時鐘觸發，隨紐約 DST 換算 UTC；不是交易日日曆判定。
Retired／非 acquisition feeds 的 schedule 為空。既有 card-level DB 時間與 freshness 狀態維持相容，
新欄位不改寫 persisted deadline 或既有 frozen plan。

## 後續發布邊界

PR4 將讓 Fetcher 每輪只使用 execution-config 該輪取得的記憶體快照，失敗時停止新日期規劃，不能回退到本機排程檔。
Prepared recovery 與 historical request／lease 不應依賴 daily schedule 成功讀取。
移除舊檔前須先解除四個 historical runners 與 backfill planner 的設定依賴。

PR2 無 DB migration，也不啟用任何 provider。沿用 schema-v4 單次部署交易與 committed receipt；
Backend metadata 隨 image 打包，Infra inventory 尚未加入 bundle allowlist。部署與 production
分階段 promotion 規則見 [deployment](../operations/deployment.md#provider-catalog-pr2-發布邊界)。
