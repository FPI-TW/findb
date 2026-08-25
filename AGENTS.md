# FinDB Agent 指南

## 專案概覽

FinDB 是一個 monorepo，包含以 FastAPI 建置的金融資料後端，以及用於監控資料導入、複查資料品質與查詢稽核資料的 TanStack Dashboard。

核心堆疊：Python 3.13、FastAPI、SQLAlchemy async、PostgreSQL、Alembic、uv、pytest、TypeScript、React、TanStack Start、pnpm。

整體資料流程是 Fetch -> Source -> durable queue -> Normalize -> Serve：

- Fetch layer：`fetcher/` 是 monorepo內的獨立 application，已有 Twelve Data、FinLab、Shioaji 三個隔離的 staging provider runtime、durable scheduler/checkpoint、contract validation 與 delivery client。Fetcher 先將 provider payload 轉成 versioned ingress contract，再 POST 到 Source API。
- Source API：`backend/app/api/v1/source.py`，負責驗證、冪等去重，並在同一 transaction 寫入 raw、run、normalization job 與 outbox。
- Durable queue：dispatcher 將 outbox 發布到 RabbitMQ，Celery worker 執行 normalization；RabbitMQ 可重建，PostgreSQL 是 durable truth。
- Normalize layer：`backend/app/services/normalize/`，把 contract/raw payload 映射到 canonical models，執行 DQ 檢查並 upsert canonical layer。
- Serve API：`backend/app/api/v1/serve.py`，只讀取 canonical layer，必須保持唯讀。

## 專案結構

```text
findb/
|- backend/                  # FastAPI 後端套件與開發工具
|  |- app/                   # API、services、models、schemas、utils、static pages
|  |  |- api/v1/             # Source ingest、Serve query、Admin routers
|  |  |- services/normalize/ # 市場別 normalizers 與 mapping logic
|  |  |- models/             # canonical、raw、registry ORM models
|  |  |- schemas/            # Pydantic request/response models
|  |  `- static/             # static mount、generated data cache
|  |- migrations/            # Alembic migration scripts
|  |- configs/               # YAML configs，例如 partial_dump.yaml
|  |- tests/                 # async API/service integration tests 與 unit tests
|  |- scripts/               # dev.py、seed scripts、partial dump、cleanup、cache gen、render nginx confs
|  |- seed/                  # local development partial dump data
|  `- pyproject.toml         # uv deps + ruff/mypy/pytest settings
|- dashboard/                # TanStack Start 營運台；監控導入、DQ 與稽核查詢
|- fetcher/                  # Provider adapter、contract validation 與 delivery client；不得 import backend/ 或連 FinDB DB
|- contracts/                # 由 backend contract registry 確定性產生的 versioned JSON Schema
|- docs/                     # 現行架構、API、維運與 backlog；索引見 docs/README.md
|- infra/nginx/              # 生產環境 nginx 設定（HTTPS、Source allowlist、Serve API key 注入）
|- docker-compose.yml        # local app + postgres + pgadmin stack
|- docker-compose.prod.yml   # 生產環境 stack；serve/ingest/dispatcher/worker/RabbitMQ/Dashboard/nginx
`- Makefile                  # monorepo workflow shortcuts，包裝 backend/scripts/dev.py
```

## 資料層

| 層級 | 儲存位置 | 保存規則 |
| --- | --- | --- |
| Raw | `raw.market_payload`，JSONB 原始 payload | 由 `RAW_RETENTION_ENABLED` 控制；預設啟用，期限設定為 `RAW_RETENTION_DAYS=30` |
| Canonical | `instruments`、`instrument_identifiers`、`trading_calendar`、`market_data_eod`、`corporate_action`、`macro_series`、`macro_observation`、`roll_rule`、`futures_contract`、`futures_continuous_eod` | 長期保存 |
| Workflow / Registry | `dataset_registry`、`ingestion_attempt`、`ingestion_run`、`normalization_job`、`normalization_outbox`、`dq_issue` | 長期保存 |

## 主要流程

1. Source API 收到 contract/raw payload，依 `idempotency_key` 去重，原子寫入 raw、run、job 與 outbox。
2. Dispatcher 發布 outbox，RabbitMQ 將 delivery 交給 Celery worker。
3. Worker 僅依 versioned schema/version 路由 contract normalizer，執行 DQ 與 canonical upsert，更新 terminal state。
4. Serve API 只讀 canonical tables，回傳查詢結果與 pagination wrapper。

## 優先查看位置

| 任務 | 位置 | 備註 |
| --- | --- | --- |
| App 啟動與生命週期 | `backend/app/main.py` | FastAPI app、lifespan DB init、router mounts、health endpoints |
| Source 寫入流程 | `backend/app/api/v1/source.py` | auth、idempotency、versioned contract ingestion 與 contract-only rerun |
| Serve 查詢流程 | `backend/app/api/v1/serve.py` | read-only query endpoints、filters、pagination |
| Admin API | `backend/app/api/v1/admin.py` | admin-only registry、run、raw payload 查詢與管理 |
| API dependencies | `backend/app/api/deps.py` | `verify_source_api_key`、`verify_serve_api_key`、IP allowlist、rate limiting |
| DB dependency | `backend/app/dependencies.py` | async session injection |
| 設定 | `backend/app/config.py` | 所有設定由 env 與 `get_settings()` 載入 |
| Ingestion orchestration | `backend/app/services/ingestion.py` | dataset/provider scope validation、run lifecycle、contract-only rerun、`CONTRACT_NORMALIZER_MAP` |
| Normalizer 基底 | `backend/app/services/normalize/base.py` | `BaseNormalizer`，所有 normalizer 的基底 |
| 市場資料標準化 | `backend/app/services/normalize/` | 市場別 mapping、DQ checks、canonical writes |
| DQ 規則 | `backend/app/services/dq/validators.py` | `severity="error"` 會阻擋寫入，`warning` 不會 |
| ORM/data model | `backend/app/models/` | raw schema、canonical tables、registry tables |
| API schemas | `backend/app/schemas/` | request/response contracts |
| Instrument cache | `backend/app/services/instrument_cache.py` + `backend/scripts/generate_instrument_cache.py` | Admin 維護與 generated JSON cache；公開查詢頁為 Dashboard `/dashboard/lookup` |
| 營運 Dashboard | `dashboard/` | TanStack Start 前端；子目錄規則見 `dashboard/AGENTS.md` |
| 測試與 fixtures | `backend/tests/` + `backend/tests/conftest.py` | AsyncClient、ASGITransport、DB dependency overrides |
| Alembic migrations | `backend/alembic.ini` + `backend/migrations/` | schema-as-code；`init_db()` 只驗證 revision，不自動建表 |
| 開發工作流 | `backend/scripts/dev.py` + `Makefile` | cross-platform local commands |
| Partial dump tooling | `backend/scripts/partial_dump.py` + `backend/configs/partial_dump.yaml` | export/import partial production data for local dev |
| Seed upsert | `backend/scripts/seed_upsert.py` | 將 partial dump CSVs 載入 local DB，支援 upsert/truncate |
| Instrument name backfill | `backend/scripts/backfill_instrument_names.py`（TW）+ `backend/scripts/backfill_world_names.py`（US/HK/CN/FX/indices） | 從 TWSE/TPEX、NASDAQ Trader、HKEX、Tencent 等公開來源補 `instruments.name` 與 `currency`；預設 dry-run，`--apply` 才寫入；TW backfill 支援 `--overwrite-existing` 清理舊版 Big5 解碼亂碼；皆為可重複執行 |
| Instrument routing 維護 | `backend/scripts/cleanup_stale_instruments.py` | 清理舊 ingest 路由錯誤殘留的 instrument 紀錄（asset_class / market 錯放、重複等） |
| 文件 | `docs/README.md` | 唯一文件入口；只保存現行架構、契約、維運規則與未完成 backlog，歷史決策由 Git history 追溯 |
| Nginx 設定樣板 | `infra/nginx/nginx.conf`、`infra/nginx/source-allowlist.conf`、`infra/nginx/cloudflare-real-ip.conf`、`infra/nginx/serve-key.conf` | 生產 nginx 主設定與三段由 deploy workflow 渲染的子設定（Source allowlist、Cloudflare real-IP、Serve API key 注入） |
| Nginx render 腳本 | `backend/scripts/render_nginx_source_allowlist.py`、`backend/scripts/render_nginx_cloudflare_real_ip.py`、`backend/scripts/render_nginx_serve_key.py` | CI/CD 部署時依 GitHub Variables/Secrets 渲染對應 `*.conf`；本機未跑時為安全 fallback |
| CI/CD 流程 | `.github/workflows/findb-ci.yml`、`.github/workflows/findb-cd.yml`、`.github/workflows/fetcher-ci.yml`、`.github/workflows/fetcher-cd.yml` | FinDB 與 Fetcher 各自獨立驗證、建置與部署；production jobs 分別綁定自己的 GitHub Environment |

## 子目錄指南

先套用本檔，再依工作目錄套用對應指南：

- `backend/AGENTS.md`：整個backend的API、models與normalize規則。
- `dashboard/AGENTS.md`：Dashboard開發規則。

若本檔與子目錄指南衝突，優先採用子目錄指南；若仍不確定，先詢問。

## 專案慣例

- 時間戳一律使用 UTC-aware datetime；使用 `app.utils.utc_now()` 與 `ensure_utc()`，ORM 欄位使用 `DateTime(timezone=True)`。
- 主鍵 UUID 使用 UUIDv7：`app.utils.uuid7()`；不得無明確需求改用 UUIDv4。
- DB access 一律 async：`AsyncSession`、`await`、明確 `commit()`。
- ORM 查詢使用 `select()`；raw SQL 必須包在 `sqlalchemy.text()`。
- Router handlers 保持薄層；business logic 放在 services。
- Source API 是 write-path ingest；Serve API 是 read-only query path。
- Pydantic v2 model 需要 ORM hydration 時使用 `from_attributes=True`。
- secrets/config 一律經由 `app.config.Settings` 與 env 載入；不可 hardcode。
- Source provider names 正規化為穩定 lowercase，例如 `twelve_data`、`finlab`、`shioaji`。
- Dependencies 由 `uv` 管理，來源是 `backend/pyproject.toml` 與 `backend/uv.lock`。
- Schema 變更一律透過 Alembic migrations；runtime `init_db()` 只檢查 Alembic revision 與 required tables，不執行 `create_all()`。
- 後端保留 `/static` mount 供 generated instrument／macro cache 使用；公開查詢頁位於 Dashboard 的 `/dashboard/lookup`。

## 專案反模式

- 在 Serve endpoints 加入寫入行為。
- 使用 naive datetime 或非 UTC timestamp。
- 未經明確需求使用 UUIDv4 作為 primary identifier。
- ingestion 寫入前跳過 dataset existence/registry checks。
- 將 DQ `severity="error"` 視為可寫入。
- 把大量 normalization 或 DB business logic 寫進 route handler。
- 直接回傳 raw ORM objects 而不經 schema shaping。
- 在 API error payload 洩漏 secrets、internal traces 或敏感設定。
- 繞過受保護 Source/Admin endpoints 的 API key dependencies。

## 專案特性

- Raw payload persistence 使用 PostgreSQL schema `raw`，核心 table 是 `raw.market_payload`。
- Normalizer routing 明確集中在 `backend/app/services/ingestion.py` 的 `CONTRACT_NORMALIZER_MAP`；缺少或不支援 schema/version 時一律 fail closed。
- Source API 只接受 versioned provider-neutral contracts；不提供 market-specific、provider-specific 或 `.../direct` compatibility routes。
- staging active provider/dataset scope 僅包含 `twelve_data/us_equity_eod`、`finlab/tw_equity_eod`、`shioaji/tw_equity_minute` 與 `shioaji/tw_etf_minute`。
- Instrument 與 macro lookup data 是 generated cache files：`backend/app/static/data/instruments.json`、`backend/app/static/data/macro-series.json`，不是 source-of-truth data。
- 生產環境 nginx 透過 `infra/nginx/serve-key.conf`（由 `backend/scripts/render_nginx_serve_key.py` 在 deploy workflow 渲染）以 exact-host Referer regex 比對，對 Dashboard `/dashboard/lookup` 觸發的 `/api/v1/serve/*` 請求自動注入 `X-API-Key`；其它來源仍 passthrough 使用者帶入的 header。
- Source allowlist、Cloudflare real-IP、Serve key 注入三組 `*.conf` 都是 deploy time 渲染；本機開發不會跑 nginx，FastAPI 自身只負責 API key、rate limit、ingest 邏輯。
- Test suite 大量使用 async fixtures、ASGITransport 與 dependency overrides。
- Rate limit state 在測試間會自動 reset。

## 新增 Normalizer

1. 在 `backend/app/schemas/ingress.py` 定義 provider-neutral versioned request schema，並發布 deterministic JSON Schema artifact。
2. 在 `backend/app/services/normalize/contracts.py` 建立 `BaseNormalizer` subclass，實作 contract mapping 與 DQ/canonical 寫入。
3. 從 `backend/app/services/normalize/__init__.py` export，並在 `backend/app/services/ingestion.py` 的 `CONTRACT_NORMALIZER_MAP` 依 schema/version 註冊。
4. 在 `backend/scripts/seed_data.py` 新增具 `allowed_sources`、defaults 與 delivery policy 的 dataset definition。
5. 在 ingress contract、canonical ingest 與市場別 test file 新增 schema、routing、rerun、DQ 與 persistence 測試。

## 安全性

- Source API：需要 DB-backed Source client 的 `X-API-Key` header。
- Admin API：需要具名 user session、DB-backed Admin machine key，或僅供復原的 break-glass key。
- Serve API：auth 可選，由 `SERVE_REQUIRE_AUTH` 控制；即使啟用 auth，Serve layer 仍必須保持唯讀。
- IP allowlist：`SOURCE_ALLOWLIST_CIDRS` 是 CIDR comma-separated list；`DEBUG=false` 時必須設定，否則 app 拒絕啟動。
- Rate limiting：in-process，keyed by API key + client IP；process restart 後狀態會重置。

## 測試環境

- 任何程式碼或 workflow 變動後，交付前必須執行與影響範圍相符的測試；修正既有測試失敗時，
  至少重跑原失敗測試與相關 test suite。靜態檢查不能取代測試執行；若受環境限制無法執行，
  必須明確記錄未執行項目與原因，不得宣稱測試通過。
- 預設測試 DB 是 `postgresql+asyncpg://findb:findb@localhost:5435/findb_test`，可用 `TEST_DATABASE_URL` 覆蓋。
- `backend/tests/conftest.py` 會在需要時建立 `findb_test` database。
- 測試資料表由 function-scope fixture 建立與清理。
- 測試 fixture 會在需要時建立 DB-backed Source/Admin credentials；`DEBUG=true`。
- Preferred test runner 是 `uv --directory backend run python scripts/dev.py test-db` 或 `make test`，會先確保 DB container 已啟動。

## 常用命令

```bash
# 安裝 dependencies
pnpm setup

# local development
make up        # migrate + seed + build + 完整啟動
make start     # Docker daemon 重啟後，以既有 images 完整啟動
make restart   # 完整停止並重啟核心 containers
make down

# iteration
make build
make migrate
make seed
make up-server
make up-db

# seed local DB from partial dump
uv --directory backend run python scripts/dev.py seed-upsert
uv --directory backend run python scripts/dev.py seed-upsert --truncate

# quality
make format
make check

# direct quality commands
uv --directory backend run ruff format app tests scripts migrations
uv --directory backend run ruff check app tests scripts migrations
uv --directory backend run mypy app

# tests
uv --directory backend run pytest
uv --directory backend run pytest tests/test_source_routes.py tests/test_canonical_ingest_api.py
uv --directory backend run pytest tests/test_canonical_ingest_api.py::test_contract_schema_endpoint_requires_source_auth
uv --directory backend run pytest -k "market_eod or market_minute"
uv --directory backend run pytest --cov=app
uv --directory backend run python scripts/dev.py test-db

# dashboard
pnpm dev:dashboard
pnpm container:dashboard
pnpm check:dashboard
pnpm build:dashboard
make dashboard-install
make dashboard-dev
make dashboard-test
make dashboard-check
make dashboard-build

# Alembic
uv --directory backend run alembic current
uv --directory backend run alembic upgrade head
uv --directory backend run alembic revision --autogenerate -m "describe change"
uv --directory backend run alembic downgrade -1
```

## 基礎設施

| Container | 說明 | Port |
| --- | --- | --- |
| `findb-app` | 本機 `APP_ROLE=all` FastAPI app | `8080` by default |
| `findb-postgres` | PostgreSQL 16 | host `5435` -> container `5432` |
| `findb-rabbitmq` | 本機 durable delivery broker | container network only |
| `findb-dispatcher` / `findb-worker` | 本機 outbox dispatcher 與 normalization worker | n/a |
| `findb-raw-cleanup` | daily raw TTL cleanup，profile: `tools` | n/a |
| `findb-pgadmin` | pgAdmin UI，profile: `tools` | `5056` -> `80` |

App container 連線 DB 使用 `db:5432`；local host 連線 DB 使用 `localhost:5435`。
Production 不使用 `findb-app` 單一角色，而是依 `docker-compose.prod.yml` 拆為
`serve`、`ingest`、`dispatcher` 與 `worker`。

## Git 行為

- 禁止在本地 merge `main`；需要整合時提 PR。
- 永遠禁止使用 `--no-verify`。
- 分支命名格式：`<type>/<summary-kebab-case>`，例如 `feat/platform-command-split`、`fix/auth-refresh-bug`。
- commit 訊息格式：`<type>: <summary>`，例如 `feat: split package scripts by platform`、`fix: guard renderer process access`。
- 發 PR 時，PR 標題、描述、變更摘要與測試說明使用繁體中文。
- `type` 建議使用：`feat`、`fix`、`refactor`、`docs`、`test`、`chore`、`build`、`ci`。

## 維護原則

- 本檔保持 repo-level 高階指南；domain/module 細節放在最接近的子目錄 `AGENTS.md`。
- 更新本檔時先確認內容符合目前 repo 結構、Makefile、scripts、tests 與 Docker 設定。
- `docs/dev/`只保存未完成的開發文件。功能完成時，必須在同一個PR將仍有效的架構、契約、
  API與維運知識整併至`docs/`對應的現行文件，並移除完成的開發文件與backlog項目；不建立
  archive，歷史由Git追溯。
- 若從其他 agent 文件整併內容，移除重複並修正過期資訊；遇到實質衝突先詢問再合併。
