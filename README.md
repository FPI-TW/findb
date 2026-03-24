# FinDB - Financial Database

Normalize + Serve 層後端服務，用於金融資料的標準化、儲存與查詢。

## 目錄

- [專案概述](#專案概述)
- [系統架構](#系統架構)
- [技術選型](#技術選型)
- [專案結構](#專案結構)
- [快速開始](#快速開始)
- [API 文件](#api-文件)
- [安全機制](#安全機制)
- [資料模型](#資料模型)
- [開發指南](#開發指南)
- [測試](#測試)
- [部署](#部署)

---

## 專案概述

FinDB 是一套可長期維護、逐步擴充的金融資料庫系統，採用三層解耦架構：

| 層級 | 職責 | 運行位置 |
|------|------|----------|
| **Fetch** | 抓取原始資料並傳送至 Source API | 外部服務（特定設備） |
| **Normalize** | 接收 Raw、標準化、DQ 檢查、寫入 Canonical | 主程式 |
| **Serve** | 提供統一 API 給報告/AI/圖表消費者 | 主程式 |

### 支援市場

| 市場代碼 | 說明 | 階段 |
|---------|------|------|
| `CRYPTO` | 加密貨幣 | Phase 1 |
| `US` | 美國股市（含指數） | Phase 1 |
| `FX` | 全球外匯 | Phase 1 |
| `MACRO` | 宏觀經濟指標 | Phase 2 |
| `WTX` | 台灣加權指數期貨 | Phase 2 |
| `GLOBAL` | 全球市場 | Phase 2 |
| `TW` | 台灣市場 | Phase 2 |
| `HK` | 香港市場 | Phase 2 |
| `CN` | 中國市場 | Phase 2 |

### 資料用途

- 隔日分析報告
- 投資研究與策略驗證
- 圖表繪製
- AI / RAG 資料正確性確認

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

| 項目 | 選擇 |
|------|------|
| 語言 | Python 3.11+ |
| 框架 | FastAPI |
| 資料庫 | PostgreSQL 16 |
| ORM | SQLAlchemy 2.0 (async) |
| 時間基準 | UTC |
| 主鍵策略 | UUID v7 |
| 套件管理 | Poetry |
| 容器化 | Docker / Docker Compose |

---

## 專案結構

```
findb/
├── app/
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
│   │   └── test_page.html      # 互動式 /test API 測試頁（含 ECharts 圖表）
│   │
│   └── utils/                  # 工具函式
│       ├── uuid7.py
│       └── datetime_utils.py
│
├── .github/
│   └── workflows/
│       └── deploy.yml          # CI/CD：test → build → deploy
│
├── scripts/
│   ├── seed_data.py            # 資料種子腳本
│   ├── cleanup_raw.py          # Raw 清理腳本
│   ├── setup_ec2.sh            # EC2 一次性初始化腳本
│   └── sample_ingest_payload.json
│
├── tests/                      # 測試
│   ├── conftest.py             # 共用 fixtures、DB override
│   ├── test_source_api.py      # Source API 測試（含安全機制）
│   ├── test_serve_api.py       # Serve API 測試
│   ├── test_normalize.py       # 正規化邏輯測試
│   ├── test_usstock_normalize.py  # 美股/區域正規化測試
│   └── test_end_to_end.py      # 端到端整合測試
│
├── docs/                       # 文件
│   ├── api_usage_guide.md      # API 使用教學（完整版）
│   ├── api_test_flow.md        # API 測試流程
│   └── api_tester.html         # 匯出的靜態 API 測試頁快照
│
├── plans/                      # 開發計劃
│   └── normalize_serve_development_plan.md
│
├── docker-compose.yml          # 本機開發環境
├── docker-compose.prod.yml     # 生產環境（AWS EC2）
├── Dockerfile
├── pyproject.toml
├── spec.md                     # 技術規格
├── roadmap.md                  # 產品路線圖
└── .env.example
```

---

## 快速開始

### 前置需求

- Python 3.11+
- Docker & Docker Compose
- Poetry

### 1. 複製環境設定

```bash
cp .env.example .env
```

編輯 `.env` 設定 API Keys：

```env
SOURCE_API_KEYS=your-source-key
SERVE_API_KEYS=your-serve-key
SERVE_REQUIRE_AUTH=false
```

> **注意**：`docker-compose.yml` 使用 `${SOURCE_API_KEYS:-dev-source-key}` 語法，
> 若 `.env` 未設定則預設使用 `dev-source-key`。本機測試可直接使用預設值。

### 2. 啟動服務（Docker）

```bash
docker-compose up -d --build
```

### 3. 初始化資料

```bash
docker-compose exec app python /app/scripts/seed_data.py
```

### 4. 驗證服務

```bash
# 健康檢查
curl http://localhost:8080/health

# API 互動式文件
open http://localhost:8080/docs

# 互動式測試頁
open http://localhost:8080/test
```

`/test` 目前提供查詢結果 JSON 檢視與時間序列圖表預覽；若回應資料符合格式，會自動以 ECharts 顯示互動圖表。

### 本機開發（不使用 Docker）

```bash
# 安裝依賴
poetry install

# 啟動 PostgreSQL（需自行準備或使用 docker-compose up db）
docker-compose up -d db

# 執行 seed
poetry run python scripts/seed_data.py

# 啟動 API
poetry run uvicorn app.main:app --reload
```

---

## API 文件

> 完整的端點規格、請求/回應格式、Python 範例與常見問題，請參閱
> **[API 使用教學](docs/api_usage_guide.md)**。

### Source API（資料攝取）

需在 Header 帶入 `X-API-Key`。

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/v1/source/ingest/crypto` | 接收 CRYPTO 市場 raw payload |
| POST | `/api/v1/source/ingest/us` | 接收 US 市場 raw payload |
| POST | `/api/v1/source/ingest/fx` | 接收 FX 市場 raw payload |
| POST | `/api/v1/source/ingest/macro` | 接收 MACRO 市場 raw payload |
| POST | `/api/v1/source/ingest/wtx` | 接收 WTX 市場 raw payload |
| POST | `/api/v1/source/ingest/global` | 接收 GLOBAL 市場 raw payload |
| POST | `/api/v1/source/ingest/tw` | 接收 TW 市場 raw payload |
| POST | `/api/v1/source/ingest/hk` | 接收 HK 市場 raw payload |
| POST | `/api/v1/source/ingest/cn` | 接收 CN 市場 raw payload |
| POST | `/api/v1/source/ingest/usstock/direct` | Bloomberg 美股直接格式 |
| POST | `/api/v1/source/ingest/hkchina/direct` | Bloomberg 港中股直接格式 |
| POST | `/api/v1/source/ingest/macro/direct` | Bloomberg 宏觀直接格式 |
| GET | `/api/v1/source/runs/{run_id}` | 查詢批次狀態 |
| POST | `/api/v1/source/runs/{run_id}/rerun` | 以原始 payload 重新執行正規化 |
| GET | `/api/v1/source/datasets` | 查詢可用資料集 |

#### 請求範例

```bash
# 標準格式攝取
curl -X POST "http://localhost:8080/api/v1/source/ingest/crypto" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@scripts/sample_ingest_payload.json"

# Direct 格式攝取（US Stock）
curl -X POST "http://localhost:8080/api/v1/source/ingest/usstock/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@bloomberg_usstock.json"
```

#### 請求格式（標準）

```json
{
  "dataset_key": "crypto_eod",
  "source": "bloomberg",
  "request_key": "bloomberg_crypto_20260116_144914",
  "idempotency_key": "bloomberg_crypto_20260116_144914",
  "payload": {
    "metadata": { "source": "Bloomberg API" },
    "data": [ ... ]
  },
  "fetched_at": "2026-01-16T14:49:14Z"
}
```

#### 請求格式（Direct）

```json
{
  "metadata": { "source": "Bloomberg API", "query_time": "2026-02-04T16:00:51" },
  "data": [ ... ]
}
```

### Serve API（資料查詢）

認證可選（由 `SERVE_REQUIRE_AUTH` 控制）。Serve API 為**唯讀**。

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/v1/serve/instruments` | 查詢標的清單（支援 market/asset_class/symbol 篩選） |
| GET | `/api/v1/serve/instruments/{id}` | 查詢單一標的 |
| GET | `/api/v1/serve/eod` | 查詢日K 資料（支援 market/symbols/日期範圍篩選） |
| GET | `/api/v1/serve/eod/{instrument_id}` | 查詢單一標的日K |
| GET | `/api/v1/serve/corporate-actions` | 查詢公司行為（除權息） |
| GET | `/api/v1/serve/corporate-actions/{instrument_id}` | 查詢單一標的公司行為 |
| GET | `/api/v1/serve/macro/series` | 查詢宏觀指標序列 |
| GET | `/api/v1/serve/macro/observations` | 查詢宏觀觀測值 |
| GET | `/api/v1/serve/macro/observations/{series_id}` | 查詢特定序列觀測值 |
| GET | `/api/v1/serve/futures/contracts` | 查詢期貨合約 |
| GET | `/api/v1/serve/futures/continuous` | 查詢連續期貨日K |
| GET | `/api/v1/serve/futures/continuous/{instrument_id}` | 查詢特定標的連續期貨日K |
| GET | `/api/v1/serve/calendar` | 查詢交易日曆（market 必填） |

#### 查詢範例

```bash
# 查詢加密貨幣標的
curl "http://localhost:8080/api/v1/serve/instruments?market=CRYPTO"

# 查詢 BTC/ETH 日K
curl "http://localhost:8080/api/v1/serve/eod?market=CRYPTO&symbols=BTC,ETH&start_date=2026-01-01&end_date=2026-01-31"

# 查詢美股除權息
curl "http://localhost:8080/api/v1/serve/corporate-actions?market=US&action_type=dividend"

# 查詢宏觀序列
curl "http://localhost:8080/api/v1/serve/macro/series?name=CPI"

# 查詢期貨合約
curl "http://localhost:8080/api/v1/serve/futures/contracts?market=WTX"

# 查詢交易日曆
curl "http://localhost:8080/api/v1/serve/calendar?market=US&start_date=2026-01-01&end_date=2026-01-31"
```

### 分頁

所有列表端點支援分頁，參數統一為 `page`（預設 1）與 `page_size`（預設 100，最大 1000）。

回應包含 `pagination` 物件：

```json
{
  "pagination": {
    "page": 1,
    "page_size": 100,
    "total_records": 2500,
    "total_pages": 25
  }
}
```

---

## 安全機制

### API Key 認證

| 層級 | 認證 | Header |
|------|------|--------|
| Source API | **必要** | `X-API-Key: {SOURCE_API_KEYS}` |
| Serve API | 可選（`SERVE_REQUIRE_AUTH`） | `X-API-Key: {SERVE_API_KEYS}` |

### IP 允許名單

Source API 支援 CIDR 格式的 IP 允許名單，透過 `SOURCE_ALLOWLIST_CIDRS` 設定。

- **開發環境**（`DEBUG=true`）：允許名單為選填，未設定時允許所有 IP
- **生產環境**（`DEBUG=false`）：允許名單為**必填**，未設定將無法啟動

```env
# 允許單一 IP
SOURCE_ALLOWLIST_CIDRS=203.0.113.50/32

# 允許子網段（逗號分隔）
SOURCE_ALLOWLIST_CIDRS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

若服務在反向代理後方，啟用 Proxy 信任：

```env
SOURCE_TRUST_PROXY_HEADERS=true
```

### 限流

Source API 限制每個 API Key + Client IP 組合的請求頻率：

| 設定 | 預設值 | 說明 |
|------|--------|------|
| `RATE_LIMIT_REQUESTS` | `100` | 時間窗口內最大請求數 |
| `RATE_LIMIT_WINDOW` | `60` | 時間窗口（秒） |

超過限制回傳 `429 Too Many Requests`。

---

## 資料模型

### Canonical Layer（長期儲存）

| 資料表 | 說明 |
|--------|------|
| `instruments` | 標的主檔（股票/指數/加密貨幣/外匯） |
| `instrument_identifiers` | 代碼映射（Bloomberg/ISIN/CUSIP） |
| `trading_calendar` | 交易日曆 |
| `market_data_eod` | 日K 資料（OHLCV） |
| `corporate_action` | 公司行為（除權息） |
| `macro_series` / `macro_observation` | 宏觀指標 |
| `futures_contract` / `futures_continuous_eod` | 期貨 |
| `dataset_registry` | 資料集定義 |
| `ingestion_run` | 攝取批次記錄 |
| `dq_issue` | 資料品質問題 |

### Raw Layer（短期儲存 14 天）

| 資料表 | 說明 |
|--------|------|
| `raw.market_payload` | 原始 JSON payload |

---

## 開發指南

### 程式碼風格

```bash
# 格式化
poetry run black app tests

# Lint
poetry run ruff check .

# 型別檢查
poetry run mypy app
```

### 新增 Normalizer

1. 在 `app/services/normalize/` 建立新檔案
2. 繼承 `BaseNormalizer`，實作 `map_fields()`
3. 在 `app/services/normalize/__init__.py` 匯出
4. 在 `app/services/ingestion.py` 的 `NORMALIZER_MAP` 加入對應
5. 在 `scripts/seed_data.py` 加入 dataset 設定

### DQ 規則

| 規則代碼 | 說明 | 嚴重程度 |
|---------|------|---------|
| OHLC_HIGH_CHECK | high >= max(open, close) | error |
| OHLC_LOW_CHECK | low <= min(open, close) | error |
| VOLUME_POSITIVE | volume >= 0 | error |
| DUPLICATE_KEY | instrument_id + trade_date 不重複 | error |
| ABNORMAL_RETURN | 單日報酬超過 ±30% | warning |
| MISSING_OHLC | OHLC 任一欄位為 null | warning |

---

## 測試

### 本機測試

```bash
# 執行所有測試
poetry run pytest

# 執行單一檔案
poetry run pytest tests/test_source_api.py

# 執行特定測試
poetry run pytest tests/test_source_api.py::TestSourceAPI::test_ingest_without_api_key

# 依關鍵字執行
poetry run pytest -k "crypto"

# 顯示覆蓋率
poetry run pytest --cov=app
```

### Docker 容器內測試

```bash
# 使用容器內的 test DB
docker-compose exec app bash -c \
  "TEST_DATABASE_URL=postgresql+asyncpg://findb:findb@db:5432/findb_test pytest"
```

### 測試環境注意事項

- 測試使用 `findb_test` 資料庫（可透過 `TEST_DATABASE_URL` 環境變數覆蓋）
- `SOURCE_API_KEYS` 必須設定否則認證測試會失敗
- 測試會自動建立/銷毀資料表
- 限流狀態在每個測試後自動重置

### 目前測試狀況

- **82 tests**（77 passed, 2 skipped）
- 涵蓋：Source 安全機制、Serve 全端點、正規化邏輯、端到端流程、Admin API

---

## 部署

### 本機開發（Docker Compose）

```bash
# 啟動
docker-compose up -d --build

# 查看日誌
docker-compose logs -f app

# 停止
docker-compose down
```

| 容器 | 說明 | 埠號 |
|------|------|------|
| `findb-app` | FastAPI 主程式 | `8000` |
| `findb-postgres` | PostgreSQL 16 | `5435` → 5432 |
| `findb-raw-cleanup` | Raw 資料每日清理 | — |
| `findb-pgadmin` | pgAdmin 管理介面 | `5056` |

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
  └── deploy   → SSH → docker compose pull & up
```

- **Image Registry**：GitHub Container Registry（ghcr.io）
- **資料庫**：AWS Aurora/RDS（EC2 不跑 PostgreSQL 容器）
- **部署方式**：SSH + `docker compose -f docker-compose.prod.yml`

#### 首次 EC2 初始化（一次性）

```bash
# 1. 安裝 Docker
sudo bash scripts/setup_ec2.sh

# 2. 複製 compose 檔到 EC2
scp -i your-key.pem docker-compose.prod.yml ubuntu@<EC2_IP>:/opt/findb/

# 3. 建立 .env（填入 RDS 連線資訊與 API Keys）
nano /opt/findb/.env

# 4. 初始化資料庫
docker exec findb-app python /app/scripts/seed_data.py
```

#### EC2 `.env` 範本

```env
DATABASE_URL=postgresql+asyncpg://user:password@your-rds.rds.amazonaws.com:5432/findb?ssl=require
DEBUG=false
SOURCE_ALLOWLIST_CIDRS=10.0.0.0/8,你的辦公室IP/32
SOURCE_API_KEYS=production-source-key
ADMIN_API_KEYS=production-admin-key
SERVE_REQUIRE_AUTH=false
RATE_LIMIT_REQUESTS=100
RATE_LIMIT_WINDOW=60
RAW_RETENTION_DAYS=14
```

#### GitHub Secrets 設定

| Secret | 說明 |
|--------|------|
| `EC2_HOST` | EC2 公開 IP 或 domain |
| `EC2_USER` | `ubuntu`（Ubuntu AMI）或 `ec2-user` |
| `EC2_SSH_KEY` | PEM 私鑰完整文字 |

#### 日常部署

```bash
# 推到 main 即自動觸發完整 CI/CD（約 3~5 分鐘）
git push origin main
```

#### 生產容器組成

| 容器 | 說明 | 埠號 |
|------|------|------|
| `findb-app` | FastAPI 主程式（2 workers） | `8000` |
| `findb-raw-cleanup` | Raw 資料每日清理 | — |

### 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `DATABASE_URL` | PostgreSQL 連線字串 | `postgresql+asyncpg://findb:findb@localhost:5435/findb` |
| `DEBUG` | 除錯模式（跳過允許名單強制檢查） | `false` |
| `SOURCE_API_KEYS` | Source API 金鑰（逗號分隔） | docker-compose 預設 `dev-source-key` |
| `ADMIN_API_KEYS` | Admin API 金鑰（逗號分隔） | （空） |
| `SOURCE_ALLOWLIST_CIDRS` | IP 允許名單（CIDR，逗號分隔） | （空）。`DEBUG=false` 時**必填** |
| `SOURCE_TRUST_PROXY_HEADERS` | 是否信任 X-Forwarded-For | `false` |
| `SERVE_API_KEYS` | Serve API 金鑰（逗號分隔） | （空） |
| `SERVE_REQUIRE_AUTH` | Serve API 是否需要認證 | `false` |
| `RATE_LIMIT_REQUESTS` | 限流上限（每 window 內的請求數） | `100` |
| `RATE_LIMIT_WINDOW` | 限流時間窗口（秒） | `60` |
| `RAW_RETENTION_DAYS` | Raw 資料保留天數 | `14` |

> **注意**：`docker-compose.yml` 使用 `${VAR:-default}` 語法讀取環境變數。
> `.env` 的設定值會生效；若未設定則使用預設值。

### 生產環境注意事項

1. **必須**設定 `SOURCE_ALLOWLIST_CIDRS`（`DEBUG=false` 時為必要）
2. **建議**啟用 `SERVE_REQUIRE_AUTH=true`
3. **建議**設定 CORS `allow_origins` 為特定網域（目前預設 `*`）
4. **建議**使用反向代理（如 nginx）處理 HTTPS
5. 限流為進程內記憶體，重啟後重置

---

## 相關文件

- **[API 使用教學](docs/api_usage_guide.md)** — 完整端點規格、範例、錯誤代碼
- [API 測試流程](docs/api_test_flow.md)
- [API 測試頁快照](docs/api_tester.html)
- [技術規格](spec.md)
- [產品路線圖](roadmap.md)
- [開發計劃](plans/normalize_serve_development_plan.md)

---

## 授權

Private - FinDB Team
