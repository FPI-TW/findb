# FinDB 文件

本目錄只保存目前仍有效的架構、契約與維運資訊。歷史決策以 Git history
追溯，不在主文件中維護已完成的 roadmap、phase checklist 或事故過程。

## 必讀文件

| 文件                                                                             | 用途                                                                       |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| [architecture/overview.md](architecture/overview.md)                             | 現行系統架構、資料流、服務角色與不可破壞的邊界。                           |
| [architecture/ingress_contracts.md](architecture/ingress_contracts.md)           | Fetcher 與 FinDB 間的 versioned ingress contract。                         |
| [architecture/service_boundaries.md](architecture/service_boundaries.md)         | Monorepo 內 FinDB、Fetcher、Dashboard 與下游服務的責任邊界。               |
| [api/api_usage_guide.md](api/api_usage_guide.md)                                 | Source、Serve、Admin API 的認證、主要端點與使用方式。                      |
| [operations/deployment.md](operations/deployment.md)                             | EC2 部署、staging資料界線、GitHub Environment、AWS IAM 與 secrets 邊界。   |
| [operations/ingestion.md](operations/ingestion.md)                               | Durable ingestion、bounded staging驗收、RabbitMQ、監控、恢復與 rollback。  |
| [operations/migration_workflow.md](operations/migration_workflow.md)             | Alembic migration 與 production rollout 規則。                             |
| [operations/data_maintenance.md](operations/data_maintenance.md)                 | Staging reset、partial dump、seed、backfill、raw retention 與 cache 維護。 |
| [dev/backlog.md](dev/backlog.md)                                                 | 尚未完成、仍需追蹤的工作與已知風險。                                       |
| [dev/fetcher-data-automation-backlog.md](dev/fetcher-data-automation-backlog.md) | Fetcher 四時段自動化、provider coverage、資料契約與上線驗收 backlog。      |

## 文件原則

- `architecture/` 描述現在或已核准的目標架構，不記錄已完成的執行過程。
- `api/` 面向 API 消費者；精確 request/response schema 以同版本 OpenAPI
  與 versioned contract endpoint 為準。
- `operations/` 只保留可直接執行的維運規則。
- `dev/backlog.md` 只列未完成項目；完成後刪除，不累積完成紀錄。
- 程式碼、Alembic migration、Compose 與 workflow 是最終 source of truth。
- 架構或部署方式改變時，必須在同一個 PR 更新相關文件。
