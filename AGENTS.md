# FinDB Agent 指南

## 專案概覽

FinDB 是以 FastAPI 建置的金融資料後端，負責接收市場資料 payload、保存 raw layer、標準化為 canonical models，並提供唯讀查詢 API。

核心堆疊：Python 3.13、FastAPI、SQLAlchemy async、PostgreSQL、Alembic、uv、pytest。

整體資料流程是 Fetch -> Normalize -> Serve：

- Fetch layer：外部資料擷取服務，不在本 repo 內，會將 Bloomberg 等 provider 的原始資料 POST 到 Source API。
- Source API：`app/api/v1/source.py`，負責驗證、冪等去重、寫入 raw layer，並觸發標準化。
- Normalize layer：`app/services/normalize/`，把 raw payload 映射到 canonical models，執行 DQ 檢查並 upsert canonical layer。
- Serve API：`app/api/v1/serve.py`，只讀取 canonical layer，必須保持唯讀。

## 專案結構

```text
findb/
|- app/                      # API、services、models、schemas、utils、static pages
|  |- api/v1/                # Source ingest、Serve query、Admin routers
|  |- services/normalize/    # 市場別 normalizers 與 mapping logic
|  |- models/                # canonical、raw、registry ORM models
|  |- schemas/               # Pydantic request/response models
|  `- static/                # /test、/instrument-lookup、generated data cache
|- migrations/               # Alembic migration scripts
|- configs/                  # YAML configs，例如 partial_dump.yaml
|- docs/                     # API guide、manual test flow、migration workflow
|- tests/                    # async API/service integration tests 與 unit tests
|- scripts/                  # dev.py、seed scripts、partial dump、cleanup、cache gen、render nginx confs
|- infra/nginx/              # 生產環境 nginx 設定（HTTPS、Source allowlist、Serve API key 注入）
|- seed/                     # local development partial dump data
|- plans/                    # architecture 與 development plans
|- docker-compose.yml        # local app + postgres + pgadmin stack
|- docker-compose.prod.yml   # 生產環境 stack；含 nginx + findb-app
|- Makefile                  # dev workflow shortcuts，包裝 scripts/dev.py
`- pyproject.toml            # uv deps + black/ruff/mypy/pytest settings
```

## 資料層

| 層級 | 儲存位置 | 保存規則 |
| --- | --- | --- |
| Raw | `raw.market_payload`，JSONB 原始 payload | 由 `RAW_RETENTION_ENABLED` 控制；預設停用，期限設定為 `RAW_RETENTION_DAYS=14` |
| Canonical | `instruments`、`instrument_identifiers`、`trading_calendar`、`market_data_eod`、`corporate_action`、`macro_series`、`macro_observation`、`roll_rule`、`futures_contract`、`futures_continuous_eod` | 長期保存 |
| Registry | `dataset_registry`、`ingestion_run`、`dq_issue` | 長期保存 |

## 主要流程

1. Source API 收到 raw payload，依 `idempotency_key` 去重，寫入 `raw.market_payload`，建立 `ingestion_run`。
2. `app/services/ingestion.py` 依 `NORMALIZER_MAP[dataset_key]` 路由到對應 normalizer。
3. Normalizer 映射欄位、執行 DQ 檢查、upsert canonical rows，並更新 `ingestion_run`。
4. Serve API 只讀 canonical tables，回傳查詢結果與 pagination wrapper。

## 優先查看位置

| 任務 | 位置 | 備註 |
| --- | --- | --- |
| App 啟動與生命週期 | `app/main.py` | FastAPI app、lifespan DB init、router mounts、health endpoints |
| Source 寫入流程 | `app/api/v1/source.py` | auth、idempotency、ingestion/rerun dispatch、direct-format endpoints |
| Serve 查詢流程 | `app/api/v1/serve.py` | read-only query endpoints、filters、pagination |
| Admin API | `app/api/v1/admin.py` | admin-only registry、run、raw payload 查詢與管理 |
| API dependencies | `app/api/deps.py` | `verify_source_api_key`、`verify_serve_api_key`、IP allowlist、rate limiting |
| DB dependency | `app/dependencies.py` | async session injection |
| 設定 | `app/config.py` | 所有設定由 env 與 `get_settings()` 載入 |
| Ingestion orchestration | `app/services/ingestion.py` | dataset validation、run lifecycle、rerun support、`NORMALIZER_MAP` |
| Normalizer 基底 | `app/services/normalize/base.py` | `BaseNormalizer`，所有 normalizer 的基底 |
| 市場資料標準化 | `app/services/normalize/` | 市場別 mapping、DQ checks、canonical writes |
| DQ 規則 | `app/services/dq/validators.py` | `severity="error"` 會阻擋寫入，`warning` 不會 |
| ORM/data model | `app/models/` | raw schema、canonical tables、registry tables |
| API schemas | `app/schemas/` | request/response contracts |
| 靜態 lookup/cache | `app/static/instrument-lookup.html` + `scripts/generate_instrument_cache.py` | `/instrument-lookup` UI 與 generated JSON cache |
| 測試頁 | `app/static/test_page.html` | static `/test` API tester；除非明確要求，避免修改 |
| 測試與 fixtures | `tests/` + `tests/conftest.py` | AsyncClient、ASGITransport、DB dependency overrides |
| Alembic migrations | `alembic.ini` + `migrations/` | schema-as-code；`init_db()` 只驗證 revision，不自動建表 |
| 開發工作流 | `scripts/dev.py` + `Makefile` | cross-platform local commands |
| Partial dump tooling | `scripts/partial_dump.py` + `configs/partial_dump.yaml` | export/import partial production data for local dev |
| Seed upsert | `scripts/seed_upsert.py` | 將 partial dump CSVs 載入 local DB，支援 upsert/truncate |
| Instrument name backfill | `scripts/backfill_instrument_names.py`（TW）+ `scripts/backfill_world_names.py`（US/HK/CN/FX/indices） | 從 TWSE/TPEX、NASDAQ Trader、HKEX、Tencent 等公開來源補 `instruments.name` 與 `currency`；預設 dry-run，`--apply` 才寫入；TW backfill 支援 `--overwrite-existing` 清理舊版 Big5 解碼亂碼；皆為可重複執行 |
| Instrument routing 修正 | `scripts/fix_misrouted_tw_futures.py` + `scripts/cleanup_stale_instruments.py` | 修整舊 ingest 路由錯誤殘留的 instrument 紀錄（asset_class / market 錯放、重複等） |
| Backfill 部署手冊 | `docs/instrument_name_backfill_deployment.md` | EC2 + Aurora 環境下執行 backfill / routing 修正的階段步驟、備份與回滾，含 MS950/CP950 編碼修正背景 |
| Ingestion 流程圖 | `docs/ingestion_workflow.html` | 從 Fetch POST 到 canonical 落地的五大階段視覺化拆解（含 DQ 與維運鉤子） |
| Cloudflare/nginx allowlist 事故筆記 | `docs/cloudflare-nginx-source-allowlist-incident.md` | 服務在 Cloudflare 後方時，nginx Source allowlist 需信任 `CF-Connecting-IP` 的 root cause 與修正 |
| Nginx 設定樣板 | `infra/nginx/nginx.conf`、`infra/nginx/source-allowlist.conf`、`infra/nginx/cloudflare-real-ip.conf`、`infra/nginx/serve-key.conf` | 生產 nginx 主設定與三段由 deploy workflow 渲染的子設定（Source allowlist、Cloudflare real-IP、Serve API key 注入） |
| Nginx render 腳本 | `scripts/render_nginx_source_allowlist.py`、`scripts/render_nginx_cloudflare_real_ip.py`、`scripts/render_nginx_serve_key.py` | CI/CD 部署時依 GitHub Variables/Secrets 渲染對應 `*.conf`；本機未跑時為安全 fallback |
| 部署流程 | `.github/workflows/deploy.yml` | GitHub Actions deploy to EC2；含 nginx confs 渲染、scp、reload 步驟 |

## 子目錄指南

先套用本檔，再套用最接近工作目錄的子目錄 `AGENTS.md`：

- `app/api/v1/AGENTS.md`
- `app/models/AGENTS.md`
- `app/services/normalize/AGENTS.md`

若本檔與子目錄指南衝突，優先採用更接近修改檔案的子目錄指南；若仍不確定，先詢問。

## 專案慣例

- 時間戳一律使用 UTC-aware datetime；使用 `app.utils.utc_now()` 與 `ensure_utc()`，ORM 欄位使用 `DateTime(timezone=True)`。
- 主鍵 UUID 使用 UUIDv7：`app.utils.uuid7()`；不得無明確需求改用 UUIDv4。
- DB access 一律 async：`AsyncSession`、`await`、明確 `commit()`。
- ORM 查詢使用 `select()`；raw SQL 必須包在 `sqlalchemy.text()`。
- Router handlers 保持薄層；business logic 放在 services。
- Source API 是 write-path ingest；Serve API 是 read-only query path。
- Pydantic v2 model 需要 ORM hydration 時使用 `from_attributes=True`。
- secrets/config 一律經由 `app.config.Settings` 與 env 載入；不可 hardcode。
- Source provider names 正規化為穩定 lowercase，例如 `bloomberg`。
- Dependencies 由 `uv` 管理，來源是 `pyproject.toml` 與 `uv.lock`。
- Schema 變更一律透過 Alembic migrations；runtime `init_db()` 只檢查 Alembic revision 與 required tables，不執行 `create_all()`。
- 靜態 UI 位於 `app/static/`，包含 `/test` 與 `/instrument-lookup`；`app/static/test_page.html` 只在明確要求時修改。

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
- Normalizer routing 明確集中在 `app/services/ingestion.py` 的 `NORMALIZER_MAP`。
- 部分市場支援 direct-format source ingest endpoints (`.../direct`)，必要時會自動 bootstrap built-in dataset registry rows。
- Macro direct payload 如果省略 market，預設為 `MACRO`。
- Instrument 與 macro lookup data 是 generated cache files：`app/static/data/instruments.json`、`app/static/data/macro-series.json`，不是 source-of-truth data。
- 生產環境 nginx 透過 `infra/nginx/serve-key.conf`（由 `scripts/render_nginx_serve_key.py` 在 deploy workflow 渲染）以 Referer regex 比對，對 `/instrument-lookup` 靜態頁觸發的 `/api/v1/serve/*` 請求自動注入 `X-API-Key`；其它來源仍 passthrough 使用者帶入的 header。
- Source allowlist、Cloudflare real-IP、Serve key 注入三組 `*.conf` 都是 deploy time 渲染；本機開發不會跑 nginx，FastAPI 自身只負責 API key、rate limit、ingest 邏輯。
- Test suite 大量使用 async fixtures、ASGITransport 與 dependency overrides。
- Rate limit state 在測試間會自動 reset。

## 新增 Normalizer

1. 在 `app/services/normalize/{market}.py` 建立 `BaseNormalizer` subclass，實作 `map_fields()`，並設定 `dataset_key`、`asset_class`、`market` 等 class attributes。
2. 從 `app/services/normalize/__init__.py` export。
3. 在 `app/services/ingestion.py` 的 `NORMALIZER_MAP` 註冊。
4. 在 `scripts/seed_data.py` 新增 dataset definition。
5. 在 `tests/test_normalize.py` 或市場別 test file 新增測試。

## 安全性

- Source API：需要 `X-API-Key` header，值需符合 `SOURCE_API_KEY`。
- Admin API：需要 `X-API-Key` header，值需符合 `ADMIN_API_KEY`。
- Serve API：auth 可選，由 `SERVE_REQUIRE_AUTH` 控制；即使啟用 auth，Serve layer 仍必須保持唯讀。
- IP allowlist：`SOURCE_ALLOWLIST_CIDRS` 是 CIDR comma-separated list；`DEBUG=false` 時必須設定，否則 app 拒絕啟動。
- Rate limiting：in-process，keyed by API key + client IP；process restart 後狀態會重置。

## 測試環境

- 預設測試 DB 是 `postgresql+asyncpg://findb:findb@localhost:5435/findb_test`，可用 `TEST_DATABASE_URL` 覆蓋。
- `tests/conftest.py` 會在需要時建立 `findb_test` database。
- 測試資料表由 function-scope fixture 建立與清理。
- 測試 fixture 會設定 `SOURCE_API_KEY=test-source-key`、`ADMIN_API_KEY=test-admin-key`、`DEBUG=true`。
- Preferred test runner 是 `uv run python scripts/dev.py test-db` 或 `make test-db`，會先確保 DB container 已啟動。

## 常用命令

```bash
# 安裝 dependencies
uv sync

# local development
make up-db
make up-server
make up
make down

# 手動啟動
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn app.main:app --reload

# Docker full stack
docker compose up -d --build
docker compose exec app python /app/scripts/seed_data.py
docker compose down

# seed local DB from partial dump
make seed-upsert
make seed-upsert-truncate

# quality
make format
make lint
make type-check
make check

# direct quality commands
uv run black app tests scripts migrations
uv run ruff check app tests scripts migrations
uv run mypy app

# tests
uv run pytest
uv run pytest tests/test_source_api.py
uv run pytest tests/test_source_api.py::TestSourceAPI::test_ingest_without_api_key
uv run pytest -k "crypto"
uv run pytest --cov=app
uv run python scripts/dev.py test-db

# Alembic
uv run alembic current
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"
uv run alembic downgrade -1
```

## 基礎設施

| Container | 說明 | Port |
| --- | --- | --- |
| `findb-app` | FastAPI app | `8080` by default |
| `findb-postgres` | PostgreSQL 16 | host `5435` -> container `5432` |
| `findb-raw-cleanup` | daily raw TTL cleanup，profile: `tools` | n/a |
| `findb-pgadmin` | pgAdmin UI，profile: `tools` | `5056` -> `80` |

App container 連線 DB 使用 `db:5432`；local host 連線 DB 使用 `localhost:5435`。

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
- 若從其他 agent 文件整併內容，移除重複並修正過期資訊；遇到實質衝突先詢問再合併。
