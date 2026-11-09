# Provider metadata

`backend/configs/provider_catalog.v1.json` 定義本次統一的四個 providers／五條 feeds：
Twelve Data US EOD、FactSet HK EOD、FinLab TW equity／ETF EOD、TAIFEX futures EOD。
Shioaji 已完成退役，不列入這份 catalog；Twelve Data 不提供 HK。

## 目前導入階段

目前完成 #399／#368 的 PR1 基礎：typed loader、生成 schema、各消費端 metadata 與 CI 檢查。
**新產物尚未接入 runtime**。既有 Backend scope、Fetcher config、Dashboard constants、Infra
inventory 仍由舊路徑消費；既有 execution catalog 與 Fetcher manifest 均為五條 feeds。
Shioaji Backend 退役與兩環境部署驗收已完成，PR2 的退役前置條件已解除。
PR1 已完成 [staging 產物與服務驗收](../operations/deployment.md#provider-catalog-pr1-staging-驗收)，
此驗收不代表已切換 Backend 排程發布或解除 historical 對舊設定的依賴。
此過渡狀態不代表已完成單一來源切換，後續工作見 [backlog](../dev/backlog.md#provider-metadata-與排程統一)。

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
| `fetcher/src/findb_fetcher/data/execution_descriptors.v1.json` | Fetcher 非排程 metadata，隨 Python package／image 打包 |
| `dashboard/src/lib/generated/provider-catalog.ts` | Typed provider constants 與 scope |
| `infra/deploy/generated/provider_inventory.v1.json` | 三 images、四 providers、十二 consumers 的明確 inventory |

資料產物包含 schema version 與 SHA-256；Backend 保存 canonical source digest，其它消費端保存
各自正向欄位投影的 digest。來源物件與集合排序不改變輸出；只改 Backend 時間不改 Fetcher、Dashboard
或 Infra 產物，避免時間調整觸發 Fetcher image／部署資產改版。
`--check` 不寫檔，任一產物缺失或內容被修改就失敗，已接入 FinDB／Fetcher CI。
Fetcher 使用正向欄位投影，不包含 schedule、deadline、date lag 或 publication retry window，避免
Backend 未來新增欄位時把排程設定一起發布到 Fetcher image。

新增／替換 feed 須先更新 catalog、確認 contract 已發布，重新生成並執行測試。消費端不可手改產物。
修改同語意欄位應只修改 catalog；目前仍未切換的 runtime 宣告在過渡期間由一致性測試約束。

## 後續發布邊界

後續 Backend 將透過唯讀、Source-authenticated execution-config API 發布 daily schedule。
Fetcher 每輪只使用該輪取得的記憶體快照，失敗時停止新日期規劃，不能回退到本機排程檔。
Prepared recovery 與 historical request／lease 不應依賴 daily schedule 成功讀取。
移除舊檔前須先解除四個 historical runners 與 backfill planner 的設定依賴。

目前沒有 API／DB migration／部署順序變更，也未啟用任何 provider。生成產物尚未加入 Infra release
bundle allowlist；那項修改與消費端切換在後續 PR 同時完成，不能用 PR1 產物宣稱已部署新排程協定。
