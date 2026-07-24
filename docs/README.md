# FinDB 文件索引

文件依用途分四類。新增文件時放入對應目錄並在此登錄；文件被取代或完成階段性任務後直接刪除（git history 保留紀錄），不留 archive。

## api/ — API 消費者文件

| 文件 | 說明 |
| --- | --- |
| [api_usage_guide.md](api/api_usage_guide.md) | API 完整使用教學：所有端點規格、請求/回應格式、認證、分頁、錯誤代碼、Python 範例。**消費者必讀的單一權威來源。** |
| [api_test_flow.md](api/api_test_flow.md) | 手動 API 測試流程：服務啟動、seed、各層測試步驟與 curl 範例、故障排查。 |

## architecture/ — 架構與規格（描述現況）

| 文件 | 說明 |
| --- | --- |
| [spec.md](architecture/spec.md) | 技術規格快照：系統架構、canonical 模型、已實作資料集、DQ 規則、目前限制。 |
| [ingestion_workflow.html](architecture/ingestion_workflow.html) | Ingestion 五大階段流程視覺化（守門點、raw 落地、背景正規化、維運鉤子、canonical 查詢）。 |
| [instrument_lookup_spec.md](architecture/instrument_lookup_spec.md) | Lookup generated cache 契約與 legacy 靜態頁原始規格。 |
| [dashboard_routes.md](architecture/dashboard_routes.md) | Dashboard 公開／受保護路由、Lookup 資料來源與 secret 邊界。 |
| [rag_supply_contract.md](architecture/rag_supply_contract.md) | RAG 衍生服務與 FinDB 的邊界：FinDB 只提供 Serve API / read replica，不承擔 embedding 或 vector index。 |
| [monorepo_environment.md](architecture/monorepo_environment.md) | 根層 pnpm workspace、單一 `.env`、Dashboard secrets 與 container 部署邊界。 |

## operations/ — 維運手冊與事故紀錄

| 文件 | 說明 |
| --- | --- |
| [migration_workflow.md](operations/migration_workflow.md) | Alembic 遷移工作流程：baseline/stamp 策略、啟動版本檢查。 |
| [rabbitmq_ingestion_runbook.md](operations/rabbitmq_ingestion_runbook.md) | EC2 RabbitMQ EBS、secrets、部署、broker 全毀恢復、監控與 rollback 手冊。 |
| [ingress_delivery_policy_runbook.md](operations/ingress_delivery_policy_runbook.md) | Canonical delivery policy、完全未送達監控、warn/reject 切換、觀測與 rollback 手冊。 |
| [instrument_name_backfill_deployment.md](operations/instrument_name_backfill_deployment.md) | EC2 + Aurora 環境執行 instrument 名稱回填與 routing 修正的部署手冊（含備份與回滾）。 |
| [cloudflare-nginx-source-allowlist-incident.md](operations/cloudflare-nginx-source-allowlist-incident.md) | 事故根因分析：Cloudflare 後方 nginx Source allowlist 需信任 `CF-Connecting-IP`。 |

## dev/ — 進行中計劃與追蹤（描述未來）

| 文件 | 說明 |
| --- | --- |
| [roadmap.md](dev/roadmap.md) | 進度摘要：已交付 / 進行中 / 下一步。 |
| [multi_asset_architecture_plan.md](dev/multi_asset_architecture_plan.md) | 多資產擴展架構計劃：ADR、schema 設計原則、多來源 ingest 規範、Phase 1-6 執行計劃。 |
| [unified_ingress_contract_plan.md](dev/unified_ingress_contract_plan.md) | 統一 ingress contract 計劃：provider-neutral dataset/schema/source 邊界、首批 EOD/期貨格式與遷移策略。 |
| [scalability_optimization_checklist.md](dev/scalability_optimization_checklist.md) | 大批量處理效能優化檢查清單（Phase 0-6），與架構計劃互補：此文件管吞吐與延遲，架構計劃管設計決策。 |
| [partial_dump_plan.md](dev/partial_dump_plan.md) | Partial dump 功能計劃與 `partial_dump.yaml` 欄位規格。 |
| [known_issues.md](dev/known_issues.md) | 架構風險清單與資料缺失排查手冊（含各項修復狀態）。 |

## 分類原則

- **api/**：給 API 消費者看的。端點行為變更時必須同步 `api_usage_guide.md`。
- **architecture/**：描述系統「現在長什麼樣」。實作改變時更新，不放未來計劃。
- **operations/**：部署、遷移、事故的操作紀錄。事故筆記寫成 root cause 格式。
- **dev/**：描述「接下來要做什麼」。完成後把結論沉澱進 architecture/ 或 operations/，計劃檔本身刪除或標記完成。

## 引用規則

- **本索引是唯一持有文件路徑的地方**。文件之間不放相對路徑連結；需要提及其他文件時只寫檔名（如 `spec.md`），讀者回到本索引定位。
- 文件搬移或改名時，只需更新本索引（與根目錄 `README.md`、`AGENTS.md` 指向本索引的入口連結），不需要掃全 repo 修連結。
