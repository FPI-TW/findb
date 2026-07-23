# FinDB Monorepo

同一個 repository 內維護 FinDB 資料服務與營運 dashboard。後端負責金融資料的導入、標準化、儲存與查詢；dashboard 用於監控導入穩定性、複查資料完整度／正確性，以及查詢稽核資料。

Monorepo 將 Python 資料服務與營運前端拆成同層 workspace；後端位於 `backend/`，前端是獨立套件 [`dashboard/`](dashboard/README.md)，Docker Compose、部署設定與共用開發入口保留在 repository 根目錄。

```text
findb/
├── backend/                      # FastAPI / PostgreSQL 資料服務
├── dashboard/                    # TanStack Start 營運台
├── infra/                        # Nginx 與部署設定
├── package.json                  # Monorepo scripts；Husky 將由此接入
├── pnpm-workspace.yaml           # Node workspace 定義
├── pnpm-lock.yaml                # 全 repository 唯一 Node lockfile
└── Makefile                      # Monorepo 統一開發入口
```

## 目錄

- [專案概述](#專案概述)
- [目前進度](#目前進度)
- [系統架構](#系統架構)
- [技術選型](#技術選型)
- [專案結構](#專案結構)
- [快速開始](#快速開始)
- [API 文件](#api-文件)
- [安全機制](#安全機制)
- [資料模型](#資料模型)
- [開發指南](#開發指南)
- [測試](#測試)
- [Git LFS](#git-lfs)
- [Git Hooks](#git-hookscommit--push-守門)
- [Migration](#migration)
- [部署](#部署)

---

## 專案概述

FinDB 是一套可長期維護、逐步擴充的金融資料庫系統，採用三層解耦架構：

| 層級          | 職責                                      | 運行位置             |
| ------------- | ----------------------------------------- | -------------------- |
| **Fetch**     | 抓取原始資料並傳送至 Source API           | 外部服務（特定設備） |
| **Normalize** | 接收 Raw、標準化、DQ 檢查、寫入 Canonical | 主程式               |
| **Serve**     | 提供統一 API 給報告/AI/圖表消費者         | 主程式               |

### 支援市場

| 市場代碼 | 說明               | 階段    |
| -------- | ------------------ | ------- |
| `CRYPTO` | 加密貨幣           | Phase 1 |
| `US`     | 美國股市（含指數） | Phase 1 |
| `FX`     | 全球外匯           | Phase 1 |
| `MACRO`  | 宏觀經濟指標       | Phase 2 |
| `WTX`    | 台灣加權指數期貨   | Phase 2 |
| `GLOBAL` | 全球市場           | Phase 2 |
| `TW`     | 台灣市場           | Phase 2 |
| `HK`     | 香港市場           | Phase 2 |
| `CN`     | 中國市場           | Phase 2 |

### 資料用途

- 隔日分析報告
- 投資研究與策略驗證
- 圖表繪製
- AI / RAG 資料正確性確認

---

## 目前進度

Source / Normalize / Serve / Admin 四條主路徑已串接完成，涵蓋 `CRYPTO` / `US` / `FX` / `TW` / `HK` / `CN` / `MACRO` / `WTX` 的 ingest（含 Bloomberg direct 格式）與查詢，測試覆蓋主流程與 end-to-end。

詳細的已交付項目（`roadmap.md`）、架構演進計劃（`multi_asset_architecture_plan.md`）與效能優化清單（`scalability_optimization_checklist.md`），見 **[docs/README.md](docs/README.md)** 文件索引。

---

## 系統架構

```
┌─────────────────┐
│   Fetch Layer   │  外部服務（不落地）
│  (特定設備)      │
└────────┬────────┘
         │ POST /api/v1/source/ingest/{market}
         ▼
┌─────────────────────────────────────────────────────┐
│                    主程式                            │
│  ┌─────────────┐   ┌─────────────┐   ┌───────────┐  │
│  │ Source API  │──▶│  Normalize  │──▶│   Serve   │  │
│  │ (認證/去重)  │   │ (Mapping/DQ)│   │  (查詢)   │  │
│  └──────┬──────┘   └──────┬──────┘   └─────┬─────┘  │
│         │                 │                │        │
│         ▼                 ▼                ▼        │
│  ┌─────────────┐   ┌─────────────┐                  │
│  │  Raw Layer  │   │  Canonical  │                  │
│  │  (14 天)    │   │  (長期)     │                  │
│  └─────────────┘   └─────────────┘                  │
└─────────────────────────────────────────────────────┘
```

---

## 技術選型

| 項目     | 選擇                    |
| -------- | ----------------------- |
| 語言     | Python 3.13+            |
| 框架     | FastAPI                 |
| 資料庫   | PostgreSQL 16           |
| ORM      | SQLAlchemy 2.0 (async)  |
| 時間基準 | UTC                     |
| 主鍵策略 | UUID v7                 |
| 套件管理 | uv（Python）/ pnpm workspace（Node） |
| 容器化   | Docker / Docker Compose |

---

## 專案結構

```
findb/
├── dashboard/                   # TanStack Start 營運台（導入、DQ、稽核）
├── backend/app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 應用入口
│   ├── config.py               # 設定管理
│   ├── dependencies.py         # 依賴注入
│   │
│   ├── api/                    # API 路由
│   │   ├── v1/
│   │   │   ├── source.py       # Source API 端點（寫入路徑）
│   │   │   ├── serve.py        # Serve API 端點（查詢路徑）
│   │   │   └── admin.py        # Admin API 端點（人工修正 / DQ）
│   │   └── deps.py             # API 依賴（認證/允許名單/限流）
│   │
│   ├── models/                 # SQLAlchemy 模型
│   │   ├── base.py             # 資料庫連線設定
│   │   ├── raw.py              # Raw Layer 模型
│   │   ├── canonical.py        # Canonical Layer 模型
│   │   ├── registry.py         # 系統註冊表模型
│   │   └── correction.py       # 人工修正紀錄模型
│   │
│   ├── schemas/                # Pydantic 模型
│   │   ├── source.py           # Source API 請求/回應
│   │   ├── serve.py            # Serve API 回應
│   │   ├── admin.py            # Admin API 請求/回應
│   │   └── common.py           # 分頁等通用模型
│   │
│   ├── services/               # 業務邏輯
│   │   ├── ingestion.py        # 資料攝取服務
│   │   ├── normalize/          # 標準化服務
│   │   │   ├── base.py         # 基礎 Normalizer
│   │   │   ├── crypto.py       # 加密貨幣
│   │   │   ├── crypto_index.py # 加密貨幣指數
│   │   │   ├── equity.py       # 股票 / 指數
│   │   │   ├── usstock.py      # 美股/全球/台灣/香港/中國 股票
│   │   │   ├── fx.py           # 外匯
│   │   │   ├── macro.py        # 宏觀經濟
│   │   │   ├── futures.py      # 期貨
│   │   │   ├── corporate_actions.py  # 公司行為
│   │   │   └── types.py        # 共用型別定義
│   │   └── dq/                 # 資料品質檢查
│   │       └── validators.py
│   │
│   ├── static/                 # 靜態檔案
│   │   ├── test_page.html      # 互動式 /test API 測試頁（含 ECharts 圖表）
│   │   ├── instrument-lookup.html  # 標的與宏觀序列查詢頁
│   │   └── data/
│   │       ├── instruments.json    # 標的靜態快取（腳本產出，不進 git）
│   │       └── macro-series.json   # 宏觀序列靜態快取（腳本產出，不進 git）
│   │
│   └── utils/                  # 工具函式
│       ├── uuid7.py
│       └── datetime_utils.py
│
├── .github/
│   └── workflows/
│       └── deploy.yml          # CI/CD：test → build → render nginx confs → deploy
│
├── infra/
│   └── nginx/                  # 生產 nginx 設定（HTTPS、Source allowlist、Serve key 注入）
│       ├── nginx.conf
│       ├── source-allowlist.conf       # deploy 時由 render 腳本產生
│       ├── cloudflare-real-ip.conf     # deploy 時由 render 腳本產生
│       └── serve-key.conf              # deploy 時由 render 腳本產生
│
├── backend/scripts/
│   ├── seed_data.py            # 資料種子腳本
│   ├── cleanup_raw.py          # Raw 清理腳本
│   ├── generate_instrument_cache.py  # 產生標的與宏觀序列查詢快取
│   ├── backfill_instrument_names.py  # TW ISIN 名稱回填（含 MS950/CP950 解碼）
│   ├── backfill_world_names.py       # US/HK/CN/FX/indices 名稱回填
│   ├── fix_misrouted_tw_futures.py   # 修正 routing bug 殘留
│   ├── render_nginx_source_allowlist.py  # 渲染 Source IP allowlist 設定
│   ├── render_nginx_cloudflare_real_ip.py # 渲染 Cloudflare real-IP 設定
│   ├── render_nginx_serve_key.py     # 渲染 Serve API key 注入設定
│   ├── setup_ec2.sh            # EC2 一次性初始化腳本
│   └── sample_ingest_payload.json
│
├── backend/tests/              # 測試
│   ├── conftest.py             # 共用 fixtures、DB override
│   ├── test_source_api.py      # Source API 測試（含安全機制）
│   ├── test_serve_api.py       # Serve API 測試
│   ├── test_normalize.py       # 正規化邏輯測試
│   ├── test_usstock_normalize.py  # 美股/區域正規化測試
│   └── test_end_to_end.py      # 端到端整合測試
│
├── docs/                       # 文件（分類索引見 docs/README.md）
│   ├── api/                    # API 使用教學、測試流程
│   ├── architecture/           # 技術規格、ingestion 流程視覺化、頁面規格
│   ├── operations/             # migration、backfill 手冊、事故紀錄
│   └── dev/                    # roadmap、進行中計劃、known issues
│
├── docker-compose.yml          # 本機開發環境
├── docker-compose.prod.yml     # 生產環境（AWS EC2）
├── backend/Dockerfile
├── backend/pyproject.toml
├── backend/migrations/
├── backend/configs/
├── backend/seed/
└── .env.example
```

---

## 快速開始

### 前置需求

- Python 3.13
- Docker & Docker Compose
- uv
- Node.js 22
- pnpm 11

### 1. 複製環境設定

```bash
cp .env.example .env
```

編輯 `.env` 設定 API Keys：

```env
SOURCE_API_KEY=your-source-key
SERVE_API_KEYS=your-serve-key
ADMIN_API_KEY=your-admin-key
DASHBOARD_USERNAME=operator
DASHBOARD_PASSWORD=your-strong-password
DASHBOARD_SESSION_SECRET=at-least-32-random-characters
FINDB_API_BASE_URL=http://localhost:8080
SERVE_REQUIRE_AUTH=false
```

根目錄 `.env` 是本機唯一環境設定入口；backend、Dashboard host process 與 Docker Compose 都使用同一份設定。Dashboard 不使用 `VITE_` secret，也不另設 `dashboard/.env`。

> **注意**：`docker-compose.yml` 使用 `${SOURCE_API_KEY:-dev-source-key}` 語法，
> 若 `.env` 未設定則預設使用 `dev-source-key`。本機測試可直接使用預設值。
> `SOURCE_ALLOWLIST_CIDRS` 由生產環境 nginx 用於限制 `/api/v1/source/*`；
> 本機直接跑 app 時不會由 FastAPI 執行 IP 允許名單。

### 1.1 初次使用或重建本機環境

若是第一次使用本專案，或需要重建乾淨的本機 DB，請在複製並調整 `.env` 後執行：

```bash
docker compose down
docker volume rm findb_postgres_data
git lfs pull
docker compose up -d db
docker compose run --rm app uv run alembic upgrade head
uv --directory backend run python scripts/dev.py seed-upsert --truncate
docker compose up -d app
```

> **注意**：`docker compose down -v` 會刪除本機 Docker volume，包含 PostgreSQL 既有資料。
> `git lfs pull` 只會把 seed CSV 拉到本機工作目錄；真正匯入 DB 的步驟是 `seed-upsert --truncate`。
> 本機 `app` container 啟動後會等待 `/health` ready，並自動產生 `/static/data/instruments.json`
> 與 `/static/data/macro-series.json`。若要關閉，設定 `FINDB_GENERATE_STATIC_CACHE_ON_STARTUP=false`。

### 2. 開發命令（固定主命令）

根目錄 `package.json` 是 monorepo 統一入口，Git hooks 與 pre-commit stages 也由此統一執行：

```bash
pnpm setup
pnpm dev:dashboard
pnpm dev:backend
pnpm container:dashboard
pnpm test
pnpm check
pnpm build
pnpm hook:pre-commit
pnpm hook:pre-push
```

後端較細部命令仍可直接使用 uv：

```bash
uv --directory backend run python scripts/dev.py up-db
uv --directory backend run python scripts/dev.py up-rabbit
uv --directory backend run python scripts/dev.py migrate
uv --directory backend run python scripts/dev.py seed-data
uv --directory backend run python scripts/dev.py up-server
uv --directory backend run python scripts/dev.py up-app
uv --directory backend run python scripts/dev.py up
uv --directory backend run python scripts/dev.py start
uv --directory backend run python scripts/dev.py restart
uv --directory backend run python scripts/dev.py build
uv --directory backend run python scripts/dev.py queue-status
uv --directory backend run python scripts/dev.py queue-logs
uv --directory backend run python scripts/dev.py test-db
uv --directory backend run python scripts/dev.py down
uv --directory backend run python scripts/dev.py partial-dump-validate
uv --directory backend run python scripts/dev.py partial-dump-run --dry-run
uv --directory backend run python scripts/dev.py seed-upsert
uv --directory backend run python scripts/dev.py seed-upsert --truncate
uv --directory backend run python scripts/dev.py seed-upsert --artifact-dir seed/partial_dump/<seed_package_name>
```

雙平台 wrapper：

```bash
# macOS / Linux
make up
make start
make restart
make build
make migrate
make seed
make up-server
make up-db
make down
make status
make logs
make test
make dashboard-install
make dashboard-dev
make dashboard-test
make format
make check
```

`make check` 是 monorepo 完整品質門檻：後端執行 Ruff、Black、mypy 與 DB-backed tests，dashboard 執行 Prettier、ESLint、TypeScript、Vitest 與 production build。如只需後端門檻，使用 `make check-backend`。

```powershell
# Windows PowerShell
.\backend\scripts\dev.ps1 up-db
.\backend\scripts\dev.ps1 up-server
.\backend\scripts\dev.ps1 up
.\backend\scripts\dev.ps1 start
.\backend\scripts\dev.ps1 restart
.\backend\scripts\dev.ps1 build
.\backend\scripts\dev.ps1 migrate
.\backend\scripts\dev.ps1 seed-data
.\backend\scripts\dev.ps1 test-db
.\backend\scripts\dev.ps1 down
.\backend\scripts\dev.ps1 partial-dump-validate
.\backend\scripts\dev.ps1 partial-dump-run --dry-run
.\backend\scripts\dev.ps1 seed-upsert
.\backend\scripts\dev.ps1 seed-upsert --truncate
.\backend\scripts\dev.ps1 seed-upsert --artifact-dir seed/partial_dump/<seed_package_name>
```

### 3. 常用開發流程

```bash
# 安裝依賴
uv --directory backend sync

# 只啟動 DB
uv --directory backend run python scripts/dev.py up-db

# 只啟動本機 server（連線本機 5435 DB）
uv --directory backend run python scripts/dev.py up-server
```

```bash
# 完整啟動 DB、RabbitMQ、app、dispatcher 與 worker；自動 migrate + seed
uv --directory backend run python scripts/dev.py up

# Docker Desktop / Docker daemon 更新或重啟後，以既有 image 完整啟動
uv --directory backend run python scripts/dev.py start

# 完整停止並依賴順序重啟全部核心 containers
uv --directory backend run python scripts/dev.py restart

# 只啟動 app + DB，不啟動 queue workers
uv --directory backend run python scripts/dev.py up-app

# 查看 queue services
uv --directory backend run python scripts/dev.py queue-status
uv --directory backend run python scripts/dev.py queue-logs

# 初始化資料（以 /seed 內 seed 包匯入可重現資料）
uv --directory backend run python scripts/dev.py seed-upsert

# 跑包含 DB 的測試
uv --directory backend run python scripts/dev.py test-db
```

日常建議直接使用 Makefile：

| 情境 | 指令 | 行為 |
| --- | --- | --- |
| 首次啟動、拉取程式更新 | `make up` | 啟動 DB/RabbitMQ，migrate、seed、build，再啟動 app/dispatcher/worker |
| Docker 更新或 daemon 重啟 | `make start` | 使用既有 images 完整啟動，不 migrate、不 build |
| 完整重啟 containers | `make restart` | 依安全順序停止並重啟全部核心 containers，不 build |
| `backend/Dockerfile` 或 dependencies 變更 | `make build` | 只重建 app/dispatcher/worker images |
| 新增或拉取 migration | `make migrate` | 啟動 DB 並執行 `alembic upgrade head` |
| dataset registry seed 變更 | `make seed` | migrate 後執行可重複的 registry seed |

`make up`、`make start`、`make restart` 的核心服務範圍皆為 DB、RabbitMQ、app、dispatcher 與 worker。`pgadmin`、`raw-cleanup` 是 tools profile，不包含在日常完整 stack。

可選工具服務（不預設啟動）：

```bash
docker compose --profile tools up -d pgadmin raw-cleanup
```

### 4. Partial Dump

```bash
# 1) 準備來源 DB 連線（唯讀）
export FINDB_REMOTE_DATABASE_URL='postgresql+asyncpg://user:pass@host:5432/dbname'

# 2) 驗證 partial_dump.yaml 契約
uv --directory backend run python scripts/dev.py partial-dump-validate

# 3) 先看規劃結果（不匯出資料）
uv --directory backend run python scripts/dev.py partial-dump-run --dry-run

# 4) 實際匯出（CSV + manifest）
uv --directory backend run python scripts/dev.py partial-dump-run

# 5) 對啟動中的本地 DB 套用初始資料（UPSERT）
uv --directory backend run python scripts/dev.py seed-upsert

# 6) 指定 seed 包版本（避免拿到最新包）
uv --directory backend run python scripts/dev.py seed-upsert --artifact-dir seed/partial_dump/prod_partial_1y_3inst_all_tables_20260422T024418Z

# 7) 大改版時重建本地資料（清空 + 刪除舊表後再 UPSERT）
uv --directory backend run python scripts/dev.py seed-upsert --truncate
```

- 設定檔：`backend/configs/partial_dump.yaml`
- 目前預設只匯出 `public` schema（不含 `raw`）
- 預設抽樣：每市場 3 檔標的、最近 1 年（依 `selection` 調整）
- `seed-upsert` 預設會選 `backend/seed/partial_dump/` 最新一包 seed 包（建議進版控），並依主鍵做 upsert 到 `DATABASE_URL`
- `seed-upsert --artifact-dir ...` 可鎖定特定 seed 包版本（參數名沿用舊稱），避免「最新包」隨時間變動
- `seed-upsert --truncate` 會先刪除與 seed 包同 schema 中「不在 seed 包內」的舊表（保留 `public.alembic_version`），再 `TRUNCATE ... RESTART IDENTITY CASCADE` 後匯入，適合本地大改版重建

<a id="git-lfs"></a>

### 4.1 Git LFS：seed CSV 管理

本專案使用 Git LFS 管理 partial dump seed 包內的 CSV，避免大型資料檔直接進入 Git 物件庫。

目前 `.gitattributes` 規則：

```gitattributes
backend/seed/**/*.csv filter=lfs diff=lfs merge=lfs -text
```

也就是說，`backend/seed/partial_dump/<seed_package_name>/tables/*.csv` 會由 Git LFS 追蹤；`manifest.json`、`schema.sql`、`load.sql` 仍是一般 Git 文字檔。

#### 第一次使用或重新 clone

```bash
# 確認本機有 Git LFS
git lfs version

# 啟用目前使用者的 Git LFS hook/filter
git lfs install

# clone 後拉下 LFS 實體檔案
git lfs pull
```

若 seed CSV 內容看起來像 `version https://git-lfs.github.com/spec/v1` 的 pointer，代表尚未拉下 LFS 物件，請執行：

```bash
git lfs pull
```

#### 確認目前 LFS 應用狀態

```bash
# 查看追蹤規則
git lfs track

# 查看目前被 LFS 管理的檔案
git lfs ls-files

# 確認指定 seed CSV 是否命中 LFS filter
git check-attr filter diff merge text -- backend/seed/partial_dump/<seed_package_name>/tables/public.market_data_eod.csv
```

正常情況下，`git check-attr` 會顯示 `filter: lfs`、`diff: lfs`、`merge: lfs`、`text: unset`。

#### 新增或更新 seed 包

```bash
# 1) 產生 partial dump seed 包
uv --directory backend run python scripts/dev.py partial-dump-run

# 2) 確認 CSV 會被 LFS 規則接住
git lfs track
git check-attr filter -- backend/seed/partial_dump/<seed_package_name>/tables/public.market_data_eod.csv

# 3) 檢查 LFS 追蹤清單
git lfs ls-files

# 4) staging：CSV 會以 LFS pointer 進 Git，實體內容由 LFS 管理
git add .gitattributes backend/seed/partial_dump/<seed_package_name>

# 5) commit
git commit -m "Add partial dump seed package"
```

如果未來需要把其他大型 seed 檔案類型納入 LFS，先更新追蹤規則，再加入檔案：

```bash
git lfs track "backend/seed/**/*.parquet"
git add .gitattributes backend/seed/
```

#### 維護注意事項

- 不要手動編輯 LFS pointer 檔；要修改資料內容時請修改原始 CSV，再重新 `git add`。
- 新增 seed CSV 前先確認 `.gitattributes` 已包含對應規則，避免大型檔案直接進入一般 Git history。
- 若切換分支後 seed CSV 遺失或仍是 pointer，執行 `git lfs pull`。
- 發現 LFS 物件缺失或毀損時可先跑 `git lfs fsck` 檢查，再重新拉取。

### 5. 驗證服務

```bash
# 健康檢查
curl http://localhost:8080/health
```

- API 文件：`http://localhost:8080/docs`
- 互動測試頁：`http://localhost:8080/test`

`/test` 目前提供查詢結果 JSON 檢視與時間序列圖表預覽；若回應資料符合格式，會自動以 ECharts 顯示互動圖表。

## Dashboard 公開工具

Dashboard Lookup 會直接呼叫後端的唯讀 `/api/v1/serve/lookup/instruments` 與
`/api/v1/serve/lookup/macro-series`，不依賴 generated cache。可透過公開路由開啟：

```text
http://localhost:3000/dashboard/lookup
```

Skill 下載與安裝說明位於 `http://localhost:3000/dashboard/skill`；需要登入的
營運台位於 `http://localhost:3000/dashboard/operations`。舊
`/instrument-lookup` 與 `/skill-install` URL 暫時保留為公開相容頁；新的
Dashboard routes 是主要入口。

舊 `/instrument-lookup` 相容頁仍使用 generated cache；只有維護該 legacy 頁時才需
手動刷新或排程執行：

```bash
uv --directory backend run python scripts/generate_instrument_cache.py
0 6 * * * cd /app && python scripts/generate_instrument_cache.py
```

---

## API 文件

> 完整的端點規格、請求/回應格式、Python 範例與常見問題，請參閱 `api_usage_guide.md`（見 [docs/README.md](docs/README.md) 索引）。

| API 群組 | 路徑前綴 | 性質 |
| ---------- | ------------------ | ------------------------------------------------------------------------- |
| Source API | `/api/v1/source/*` | 資料攝取：標準/direct ingest、run status、rerun、datasets（**必要** `X-API-Key`） |
| Serve API  | `/api/v1/serve/*`  | **唯讀**查詢：instruments、EOD、corporate actions、macro、futures、calendar（認證可選，`SERVE_REQUIRE_AUTH`） |
| Admin API  | `/api/v1/admin/*`  | DQ issue、EOD patch、更正紀錄、raw payload、bulk rerun、instrument cache（**必要** `X-API-Key`） |

快速範例：

```bash
# 標準格式攝取
curl -X POST "http://localhost:8080/api/v1/source/ingest/crypto" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@backend/scripts/sample_ingest_payload.json"

# 查詢日K
curl "http://localhost:8080/api/v1/serve/eod?market=CRYPTO&symbols=BTC,ETH&start_date=2026-01-01&end_date=2026-01-31"
```

所有列表端點支援分頁：`page`（預設 1）、`page_size`（預設 100，最大 1000），回應含 `pagination` 物件。

---

## 安全機制

### API Key 認證

| 層級       | 認證                         | Header                         |
| ---------- | ---------------------------- | ------------------------------ |
| Source API | **必要**                     | `X-API-Key: {SOURCE_API_KEY}` |
| Serve API  | 可選（`SERVE_REQUIRE_AUTH`） | `X-API-Key: {SERVE_API_KEYS}`  |
| Admin API  | **必要**                     | `X-API-Key: {ADMIN_API_KEY}`  |

> **生產環境 Serve API key 注入**：`/dashboard/lookup` 公開頁不會把 Serve API key
> 嵌入瀏覽器，而是由生產 nginx 依 `Referer` 比對後注入 `X-API-Key`（設定來自
> `backend/scripts/render_nginx_serve_key.py` 在 deploy 時渲染的 `infra/nginx/serve-key.conf`）。
> 外部呼叫者若自行帶 `X-API-Key`，passthrough 行為不受影響。

### IP 允許名單

Source API 的 IP 允許名單在生產環境由 nginx 執行，透過 `SOURCE_ALLOWLIST_CIDRS` 產生 `/api/v1/source/*` 專用的 nginx `allow` / `deny` 規則。產生時會固定允許本機 loopback（`127.0.0.1/32`、`::1/128`），再加上 `SOURCE_ALLOWLIST_CIDRS` 指定的 IP/CIDR。FastAPI 層仍負責 API Key、rate limit 與 ingest 邏輯。

- **生產環境**：`SOURCE_ALLOWLIST_CIDRS` 為**必填**，部署流程會渲染 `source-allowlist.conf`
- **本機直接跑 app**：不執行 nginx IP 允許名單；需要驗證阻擋行為時，請透過 nginx 或部署環境測試

```env
# 允許單一外部 IP（本機 loopback 會自動允許）
SOURCE_ALLOWLIST_CIDRS=203.0.113.50

# 允許子網段（逗號分隔）
SOURCE_ALLOWLIST_CIDRS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

若服務在反向代理後方，啟用 Proxy 信任：

```env
SOURCE_TRUST_PROXY_HEADERS=true
```

nginx 會覆寫 `X-Real-IP` 與 `X-Forwarded-For` 為實際來源 IP；Source API 的進站阻擋在請求到達 FastAPI 前完成。

### 限流

Source API 限制每個 API Key + Client IP 組合的請求頻率：

| 設定                  | 預設值 | 說明                 |
| --------------------- | ------ | -------------------- |
| `RATE_LIMIT_REQUESTS` | `100`  | 時間窗口內最大請求數 |
| `RATE_LIMIT_WINDOW`   | `60`   | 時間窗口（秒）       |

超過限制回傳 `429 Too Many Requests`。

---

## 資料模型

### Canonical Layer（長期儲存）

| 資料表                                        | 說明                                |
| --------------------------------------------- | ----------------------------------- |
| `instruments`                                 | 標的主檔（股票/指數/加密貨幣/外匯） |
| `instrument_identifiers`                      | 代碼映射（Bloomberg/ISIN/CUSIP）    |
| `trading_calendar`                            | 交易日曆                            |
| `market_data_eod`                             | 日K 資料（OHLCV）                   |
| `corporate_action`                            | 公司行為（除權息）                  |
| `macro_series` / `macro_observation`          | 宏觀指標                            |
| `futures_contract` / `futures_continuous_eod` | 期貨                                |
| `canonical_correction`                        | Admin 人工修正不可變稽核紀錄        |
| `dataset_registry`                            | 資料集定義                          |
| `ingestion_run`                               | 攝取批次記錄                        |
| `dq_issue`                                    | 資料品質問題                        |

### Raw Layer（清理策略可配置）

| 資料表               | 說明              |
| -------------------- | ----------------- |
| `raw.market_payload` | 原始 JSON payload |

---

## 開發指南

### 程式碼風格

```bash
# 格式化
uv --directory backend run black app tests scripts

# Lint
uv --directory backend run ruff check .

# 型別檢查
uv --directory backend run mypy app
```

### 新增 Normalizer

1. 在 `backend/app/services/normalize/` 建立新檔案
2. 繼承 `BaseNormalizer`，實作 `map_fields()`
3. 在 `backend/app/services/normalize/__init__.py` 匯出
4. 在 `backend/app/services/ingestion.py` 的 `NORMALIZER_MAP` 加入對應
5. 在 `backend/scripts/seed_data.py` 加入 dataset 設定

### DQ 規則

| 規則代碼        | 說明                              | 嚴重程度 |
| --------------- | --------------------------------- | -------- |
| OHLC_HIGH_CHECK | high >= max(open, close)          | error    |
| OHLC_LOW_CHECK  | low <= min(open, close)           | error    |
| VOLUME_POSITIVE | volume >= 0                       | error    |
| DUPLICATE_KEY   | instrument_id + trade_date 不重複 | error    |
| ABNORMAL_RETURN | 單日報酬超過 ±30%                 | warning  |
| MISSING_OHLC    | OHLC 任一欄位為 null              | warning  |

---

## 測試

### 本機測試

```bash
# 標準命令（先確保 DB 可用）
uv --directory backend run python scripts/dev.py test-db

# 執行所有測試
uv --directory backend run pytest

# 執行單一檔案
uv --directory backend run pytest tests/test_source_api.py

# 執行特定測試
uv --directory backend run pytest tests/test_source_api.py::TestSourceAPI::test_ingest_without_api_key

# 依關鍵字執行
uv --directory backend run pytest -k "crypto"

# 顯示覆蓋率
uv --directory backend run pytest --cov=app
```

### Docker 容器內測試

```bash
# 使用容器內的 test DB
docker compose exec app bash -c \
  "TEST_DATABASE_URL=postgresql+asyncpg://findb:findb@db:5432/findb_test pytest"
```

### 測試環境注意事項

- 測試使用 `findb_test` 資料庫（可透過 `TEST_DATABASE_URL` 環境變數覆蓋）
- `SOURCE_API_KEY` 必須設定否則認證測試會失敗
- 測試會自動建立/銷毀資料表
- 限流狀態在每個測試後自動重置

### 目前測試狀況

- 已建立完整 pytest 測試組合，涵蓋：
- Source API 安全機制、標準 ingest、direct ingest、rerun、allowlist、rate limit
- Serve API 全端點查詢
- Admin API 修正、DQ issue、raw payload、bulk rerun、instrument cache
- 正規化邏輯與端到端 ingest -> normalize -> serve 流程

## Git Hooks（commit / push 守門）

```bash
# 安裝 monorepo dependencies 與 pre-commit / pre-push hooks
pnpm setup

# 手動執行與 Git hooks 完全相同的檢查
pnpm hook:pre-commit
pnpm hook:pre-push

# 只重新安裝 Git hooks
uv --directory backend run pre-commit install \
  --hook-type pre-commit \
  --hook-type pre-push
```

目前規則：

- pre-commit 同時是 Git hook 管理器與唯一檢查規則來源，設定集中於 `.pre-commit-config.yaml`。
- `pre-commit`：對 Python staged files 執行 Ruff fix 與 Black，Dashboard 有變更時執行 Prettier check 與 ESLint。
- `pre-push`：執行 `make check`，包含後端 lint、format、mypy、DB tests，以及 Dashboard format、lint、typecheck、tests 與 production build。
- 不再使用 Husky；`pnpm setup` 會直接將兩個 hooks 安裝到 Git。

## Migration

- 操作流程與 baseline/stamp 策略請見 `migration_workflow.md`（docs 索引）
- 常用命令：`uv --directory backend run alembic current`、`uv --directory backend run alembic revision --autogenerate -m \"...\"`、`uv --directory backend run alembic upgrade head`
- 啟動時不再自動 `create_all()`；若資料庫版本未到 `head` 或缺核心表，服務會直接啟動失敗並提示先跑 migration。

---

## 部署

### 本機開發（Docker Compose）

```bash
# 啟動
docker compose up -d --build app db

# 查看日誌
docker compose logs -f app

# 停止
docker compose down
```

| 容器                | 說明             | 埠號          |
| ------------------- | ---------------- | ------------- |
| `findb-app`         | FastAPI 主程式   | `8080`        |
| `findb-postgres`    | PostgreSQL 16    | `5435` → 5432 |
| `findb-raw-cleanup` | Raw 資料每日清理 | —             |
| `findb-pgadmin`     | pgAdmin 管理介面 | `5056`        |

---

### 生產環境（AWS EC2 + CI/CD）

#### 架構

```
git push origin main
       │
       ▼
GitHub Actions
  ├── test     → pytest（postgres service container）
  ├── build    → docker build + push ghcr.io/fpi-tw/findb
  └── deploy   → render infra/nginx/*.conf (Source allowlist / Cloudflare real-IP /
                 Serve key 注入) → scp 到 EC2 → docker compose pull & up → nginx reload
```

- **Image Registry**：GitHub Container Registry（ghcr.io）
- **資料庫**：AWS Aurora/RDS（EC2 不跑 PostgreSQL 容器）
- **部署方式**：SSH + `docker compose -f docker-compose.prod.yml`

#### 首次 EC2 初始化（一次性）

```bash
# 1. 安裝 Docker
sudo bash backend/scripts/setup_ec2.sh

# 2. 在 GitHub Actions 設定下方 Secrets / Variables

# 3. push 到 main，由 GitHub Actions 同步 compose 並部署
git push origin main
```

> 生產環境不再使用 `/opt/findb/.env`。`docker-compose.prod.yml` 會由 GitHub Actions 在 SSH 部署時注入所有環境變數。部署前請先完成 Alembic baseline/stamp 與 migration rollout。

#### GitHub Secrets 設定

| Secret                | 說明                                                                     |
| --------------------- | ------------------------------------------------------------------------ |
| `EC2_HOST`            | EC2 公開 IP 或 domain                                                    |
| `EC2_USER`            | `ubuntu`（Ubuntu AMI）或 `ec2-user`                                      |
| `EC2_SSH_KEY`         | PEM 私鑰完整文字                                                         |
| `DATABASE_URL`        | RDS / Aurora PostgreSQL 連線字串                                         |
| `SOURCE_API_KEY`     | Source API 金鑰                                                          |
| `ADMIN_API_KEY`      | Admin API 金鑰                                                           |
| `DASHBOARD_USERNAME` | Dashboard 唯一登入帳號                                                   |
| `DASHBOARD_PASSWORD` | Dashboard 登入密碼                                                       |
| `DASHBOARD_SESSION_SECRET` | Dashboard session 簽章密鑰，至少 32 個隨機字元                    |
| `SERVE_API_KEYS`      | Serve API 金鑰（`SERVE_REQUIRE_AUTH=true` 時必填）。Deploy workflow 會以**第一個** key 渲染 `infra/nginx/serve-key.conf` 注入給 `/instrument-lookup` 同源請求 |
| `FINDB_STATIC_CACHE_SERVE_API_KEY` | 產生靜態查詢快取使用的 Serve API key（`SERVE_REQUIRE_AUTH=true` 時必填） |

#### GitHub Variables 設定

| Variable                     | 建議值 / 說明                                                                |
| ---------------------------- | ---------------------------------------------------------------------------- |
| `APP_NAME`                   | `FinDB`                                                                      |
| `APP_VERSION`                | `0.1.0`                                                                      |
| `DEBUG`                      | 生產環境使用 `false`                                                         |
| `PORT`                       | `8080`                                                                       |
| `DATABASE_POOL_SIZE`         | `5`                                                                          |
| `DATABASE_MAX_OVERFLOW`      | `10`                                                                         |
| `API_V1_PREFIX`              | `/api/v1`                                                                    |
| `API_KEY_HEADER`             | `X-API-Key`                                                                  |
| `SOURCE_ALLOWLIST_CIDRS`     | nginx Source API 允許名單，例如 `10.0.0.0/8,203.0.113.50/32`；本機 loopback 會自動加入 |
| `SOURCE_TRUST_PROXY_HEADERS` | 是否信任反向代理 header，例如 `false`                                        |
| `SERVE_REQUIRE_AUTH`         | Serve API 是否需要認證，例如 `false`                                         |
| `RATE_LIMIT_REQUESTS`        | `100`                                                                        |
| `RATE_LIMIT_WINDOW`          | `60`                                                                         |
| `RAW_RETENTION_ENABLED`      | 是否啟用 raw 清理，例如 `false`                                              |
| `RAW_RETENTION_DAYS`         | `14`                                                                         |
| `FINDB_STATIC_CACHE_BASE_URL` | 靜態快取產生腳本呼叫 Serve API 的 base URL；部署容器內預設 `http://127.0.0.1:8080` |
| `FINDB_LATEST_PRICE_WORKERS` | 靜態快取查詢最新價格的並行數，例如 `12`                                      |

#### 日常部署

```bash
# 推到 main 即自動觸發完整 CI/CD（約 3~5 分鐘）
git push origin main
```

#### 生產容器組成

| 容器                | 說明                        | 埠號   |
| ------------------- | --------------------------- | ------ |
| `findb-app`         | FastAPI 主程式（2 workers） | `8080` |
| `findb-raw-cleanup` | Raw 資料每日清理            | —      |

### 環境變數

| 變數                         | 說明                             | 預設值                                                         |
| ---------------------------- | -------------------------------- | -------------------------------------------------------------- |
| `DATABASE_URL`               | PostgreSQL 連線字串              | `postgresql+asyncpg://findb:findb@localhost:5435/findb`        |
| `DEBUG`                      | 除錯模式                         | `false`                                                        |
| `SOURCE_API_KEY`            | Source API 金鑰                  | `docker compose` 開發環境預設 `dev-source-key`                 |
| `ADMIN_API_KEY`             | Admin API 金鑰                   | （空）                                                         |
| `SOURCE_ALLOWLIST_CIDRS`     | nginx `/api/v1/source/*` IP 允許名單（CIDR，逗號分隔） | `127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` |
| `SOURCE_TRUST_PROXY_HEADERS` | 是否信任 X-Forwarded-For（rate limit client IP 用） | `false`                                                        |
| `SERVE_API_KEYS`             | Serve API 金鑰（逗號分隔）       | （空）                                                         |
| `SERVE_REQUIRE_AUTH`         | Serve API 是否需要認證           | `false`                                                        |
| `FINDB_GENERATE_STATIC_CACHE_ON_STARTUP` | 本機 Docker app 啟動後是否自動產生查詢頁 JSON 快取 | `true` |
| `FINDB_STATIC_CACHE_BASE_URL` | 產生靜態查詢快取時使用的 FindDB API base URL | `http://127.0.0.1:8080` |
| `FINDB_STATIC_CACHE_SERVE_API_KEY` | 產生靜態查詢快取時使用的 Serve API key | （空） |
| `FINDB_STATIC_CACHE_STARTUP_ATTEMPTS` | 本機 Docker app 啟動後等待 health ready 的重試次數 | `24` |
| `RATE_LIMIT_REQUESTS`        | 限流上限（每 window 內的請求數） | `100`                                                          |
| `RATE_LIMIT_WINDOW`          | 限流時間窗口（秒）               | `60`                                                           |
| `RAW_RETENTION_ENABLED`      | 是否啟用 Raw 過期清理            | `false`                                                        |
| `RAW_RETENTION_DAYS`         | Raw 資料保留天數                 | `14`                                                           |

> **注意**：本機 `docker-compose.yml` 仍可讀取 `.env` 或 shell 環境變數；生產 `docker-compose.prod.yml` 只接受 GitHub Actions 在部署時傳入的環境變數。
> 目前預設不會自動刪除 raw payload；正式上線時再將 `RAW_RETENTION_ENABLED=true` 啟用即可。

### 生產環境注意事項

1. **必須**設定 `SOURCE_ALLOWLIST_CIDRS`，部署流程會用它產生 nginx `/api/v1/source/*` allowlist
2. **建議**啟用 `SERVE_REQUIRE_AUTH=true`
3. **建議**設定 CORS `allow_origins` 為特定網域（目前預設 `*`）
4. **建議**使用反向代理（如 nginx）處理 HTTPS
5. 限流為進程內記憶體，重啟後重置

---

## 相關文件

所有文件由 **[docs/README.md](docs/README.md)** 統一索引，依用途分為：

- `api/` — API 使用教學、測試流程
- `architecture/` — 技術規格、流程視覺化、頁面規格
- `operations/` — migration、backfill 手冊、事故紀錄
- `dev/` — roadmap、進行中計劃、known issues

---

## 授權

Private - FinDB Team
