# FinDB

FinDB 是金融資料 ingestion、normalization、data quality 與 canonical query
平台。Repository 包含FastAPI backend、TanStack Dashboard、versioned contracts
與獨立Fetcher套件；Fetcher已有Twelve Data日線adapter、獨立image與CI/CD，
production EC2、GitHub Environment與部署權限仍須在外部建立。

## 架構

```text
Fetcher
  -> Versioned Ingress Contract
  -> Source API
  -> Attempt + Raw + Run + Job + Transactional Outbox
  -> Dispatcher -> RabbitMQ -> Celery Worker
  -> Normalize + DQ + Canonical PostgreSQL
  -> Serve API / Admin API / Dashboard
```

- Source API只有在durable state commit後才回 `202 Accepted`。
- RabbitMQ是可重建的delivery layer；PostgreSQL是workflow truth。
- Production將serve、ingest、dispatcher與worker拆成不同process/container。
- Serve API維持唯讀。
- 所有新feed先轉成provider-neutral versioned contract，不能直接寫DB。
- Schema變更只透過Alembic。

完整架構見 [docs/architecture/overview.md](docs/architecture/overview.md)。

## Repository

```text
findb/
├── backend/
│   ├── app/
│   │   ├── api/v1/              Source、Serve、Admin routes
│   │   ├── models/              ORM models
│   │   ├── schemas/             API與ingress contracts
│   │   └── services/            Ingestion、queue、normalize、DQ
│   ├── migrations/              Alembic revisions
│   ├── tests/                   Backend tests
│   ├── scripts/                 Dev、seed、maintenance、deploy helpers
│   └── configs/                 Maintenance configs
├── dashboard/                   TanStack營運台
├── fetcher/                     Provider adapter、contract validation與delivery client
├── contracts/                   由backend registry產生的不可變契約
├── docs/                        現行架構、API、維運與backlog
├── infra/nginx/                 Production nginx設定
├── docker-compose.yml           Local stack
├── docker-compose.prod.yml      Production stack
├── Makefile                     主要開發入口
└── pnpm-workspace.yaml
```

Fetcher目前包含contract validation、安全delivery client、Twelve Data
`market_eod.v1`日線adapter、durable scheduler、checkpoint與staging runtime。詳見
[服務邊界](docs/architecture/service_boundaries.md)。

## 技術

| 項目 | 選擇 |
| --- | --- |
| Backend | Python 3.13、FastAPI、SQLAlchemy async |
| Database | PostgreSQL 16、Alembic |
| Queue | RabbitMQ、Celery |
| Frontend | TypeScript、React、TanStack Start |
| Tooling | uv、pnpm、pytest、ruff、mypy、Vitest |
| Deployment | Docker Compose、GHCR、GitHub Actions、AWS EC2/Aurora |
| Time/ID | UTC-aware datetime、UUIDv7 |

## 本機開始

需求：

- Python 3.13
- uv
- Node.js 22+
- pnpm 11
- Docker

安裝dependency與Git hooks：

```bash
pnpm setup
```

建立 `.env`：

```bash
cp .env.example .env
```

啟動完整stack：

```bash
make up
make status
```

主要入口：

| 服務 | URL |
| --- | --- |
| API health | `http://localhost:8080/health` |
| OpenAPI | `http://localhost:8080/docs` |
| Dashboard container | `http://localhost:3333/dashboard/` |
| Dashboard dev server | `http://localhost:3000/dashboard/` |
| PostgreSQL | `localhost:5435` |

停止stack：

```bash
make down
```

## 常用工作流

```bash
# Stack
make up
make start
make restart
make down
make build
make status
make logs

# Backend iteration
make up-db
make migrate
make seed
make up-server

# Dashboard
make dashboard-install
make dashboard-dev
make dashboard-test
make dashboard-check
make dashboard-build

# Fetcher
make fetcher-test
make fetcher-check
make fetcher-build

# Quality
make format
make check
make test
```

直接執行：

```bash
uv --directory backend run pytest
uv --directory backend run pytest tests/test_source_api.py
uv --directory backend run ruff check app tests scripts migrations
uv --directory backend run ruff format --check app tests scripts migrations
uv --directory backend run mypy app
```

## API

| API | Prefix | 認證 | 邊界 |
| --- | --- | --- | --- |
| Source | `/api/v1/source` | 必須 `X-API-Key` | 接受delivery與查run，不直接暴露DB |
| Serve | `/api/v1/serve` | 由 `SERVE_REQUIRE_AUTH` 控制 | 唯讀canonical query |
| Admin | `/api/v1/admin` | 必須 `X-API-Key` | DQ、修正、raw audit、keys與queue health |

新Fetcher只使用：

```text
POST /api/v1/source/ingest
```

目前已發布：

- `market_eod.v1`
- `futures_continuous_eod.v1`

Machine-readable contract：

```text
GET /api/v1/source/contracts/{schema_id}/versions/{schema_version}
```

穩定使用規則見 [API指南](docs/api/api_usage_guide.md)；精確request/response schema
以部署版本的 `/docs` 與 `/openapi.json` 為準。

## 資料與安全邊界

- Raw：`raw.market_payload`，retention預設停用。
- Workflow：attempt、run、job與outbox長期提供audit/recovery。
- Canonical：instrument、calendar、EOD、corporate action、macro、futures、bonds。
- Source/Admin keys不得提供給一般Serve consumer。
- Fetcher不得取得FinDB DB、RabbitMQ或Admin credentials。
- Secrets只能經Settings與環境注入，不得hardcode。
- Source production入口由API key、client scope、rate limit與nginx allowlist共同保護。

## Deployment

CI/CD已拆成FinDB CI、FinDB CD、Fetcher CI與Fetcher CD四個獨立workflow。FinDB
deployment unit建置backend與Dashboard images，並透過
`docker-compose.prod.yml`部署以下服務：

- `serve`
- `ingest`
- `dispatcher`
- `worker`
- `rabbitmq`
- `rabbitmq-policy`
- `dashboard`
- `nginx`
- `raw-cleanup`

Fetcher CD發布 `ghcr.io/fpi-tw/findb-fetcher:<sha>`並讓三個scheduler container在獨立
target常駐；scheduler是否開始新cycle由FinDB DB控制，owner透過Dashboard變更。
Migration在各environment都以`stopped`建立控制資料，再於受控部署驗證後逐一啟用。

Staging只用於有明確symbol、日期與筆數上限的端到端功能驗證，不導入完整
universe或production-scale歷史資料。完整限制與例外核准方式見
[Deployment staging data policy](docs/operations/deployment.md#staging-data-policy)。

部署設定由 `infra/env/` 的service-specific安全來源管理；實際值不進Git。仍需完成：

- 建立production Environments、protection rules與獨立資源；staging isolation已完成。
- GitHub Actions改用AWS OIDC短效權限。
- Runtime secrets搬到AWS Secrets Manager/Parameter Store。
- FinDB與Fetcher使用不同deploy roles、instance roles與secret paths。

Contract變更會執行兩個CI，但contract-only變更不會自動部署Fetcher；發布採
backend-first，必要時以 `workflow_dispatch`明確啟動各CD。

現況、目標權限矩陣、migration與rollback規則見
[Deployment](docs/operations/deployment.md)。

## 文件

唯一文件入口：[docs/README.md](docs/README.md)

文件只保存目前有效內容：

- `architecture/`：現行或已核准的目標架構。
- `api/`：穩定API使用規則。
- `operations/`：可直接執行的維運規則。
- `dev/backlog.md`：未完成工作；完成後刪除。

歷史roadmap、完成的phase checklist與事故過程由Git history追溯，不在主文件保留。

## Git

- 禁止在本地merge `main`；整合一律提PR。
- Branch：`<type>/<summary-kebab-case>`。
- Commit：`<type>: <summary>`。
- 永遠禁止 `--no-verify`。
- PR標題、描述、變更摘要與測試說明使用繁體中文。

## License

Private — FinDB Team
