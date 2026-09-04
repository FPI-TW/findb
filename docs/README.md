# FinDB 文件

本目錄只保存目前有效的架構、契約、操作規則與未完成工作。歷史決策、已完成計畫與
一次性cutover由Git history追溯，不在主文件持續維護。

## 從任務開始

| 需求 | 文件 |
| --- | --- |
| 理解整體資料流、服務責任、active feeds與scheduler/calendar治理 | [architecture/overview.md](architecture/overview.md) |
| 實作或升級Fetcher與Source間的versioned contract | [architecture/ingress_contracts.md](architecture/ingress_contracts.md) |
| 呼叫Source、Serve或Admin API | [api/api_usage_guide.md](api/api_usage_guide.md) |
| 部署、migration、credential設定、release或rollback | [operations/deployment.md](operations/deployment.md) |
| 管理staging原生／custom告警、SNS確認、collector與synthetic通知測試 | [operations/monitoring.md](operations/monitoring.md) |
| 驗證ingestion、監控queue、診斷delivery或重建RabbitMQ | [operations/ingestion.md](operations/ingestion.md) |
| 執行partial dump、seed、backfill、cache、reset或raw retention | [operations/data_maintenance.md](operations/data_maintenance.md) |
| 查看尚未完成且已核准的工作 | [dev/backlog.md](dev/backlog.md) |
| 查看尚未完成的staging deployment hardening、active-feed與DLM時間型exit gates | [dev/staging-aws-deployment-plan.md](dev/staging-aws-deployment-plan.md) |

## Source of truth

文件用來解釋穩定邊界與操作意圖；精確行為依下列來源為準：

- API與schema：部署版本的OpenAPI、versioned contract endpoint及`contracts/`。
- Active datasets與policy：`backend/scripts/seed_data.py`及Fetcher的versioned configs。
- DB schema：ORM與Alembic migrations。
- Runtime topology：`docker-compose.prod.yml`。
- Environment設定：`infra/env/`與`app.config.Settings`。
- CI/CD：`.github/workflows/`。

## 維護原則

- `architecture/`只描述現況與不可破壞的邊界。
- `api/`只保存穩定使用規則，不複製完整OpenAPI。
- `operations/`只保存可重複執行的runbook；一次性cutover完成後刪除。
- `dev/backlog.md`只列仍在核准範圍內的未完成工作，完成後直接移除。
- `dev/`只保存尚未完成的開發計畫。功能完成時，必須在同一個PR將仍有效的架構、契約、API
  或操作知識整併至對應的`architecture/`、`api/`或`operations/`文件，並移除完成的開發文件；
  不建立archive或保留已完成計畫，歷史由Git追溯。
- 架構或部署方式改變時，在同一個PR更新相關文件並移除被取代的描述。
