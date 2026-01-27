# FinDB - Financial Database

Normalize + Serve 層後端服務，用於金融資料的標準化、儲存與查詢。

## 目錄

- [專案概述](#專案概述)
- [系統架構](#系統架構)
- [技術選型](#技術選型)
- [專案結構](#專案結構)
- [快速開始](#快速開始)
- [API 文件](#api-文件)
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

### 支援市場（Phase 1）

- 加密貨幣（Crypto）
- 美股（US Equity / Index）
- 全球外匯（FX）

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
         │ POST /api/v1/source/ingest
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
│   │   │   ├── source.py       # Source API 端點
│   │   │   └── serve.py        # Serve API 端點
│   │   └── deps.py             # API 依賴（認證等）
│   │
│   ├── models/                 # SQLAlchemy 模型
│   │   ├── base.py             # 資料庫連線設定
│   │   ├── raw.py              # Raw Layer 模型
│   │   ├── canonical.py        # Canonical Layer 模型
│   │   └── registry.py         # 系統註冊表模型
│   │
│   ├── schemas/                # Pydantic 模型
│   │   ├── source.py
│   │   ├── serve.py
│   │   └── common.py
│   │
│   ├── services/               # 業務邏輯
│   │   ├── ingestion.py        # 資料攝取服務
│   │   ├── normalize/          # 標準化服務
│   │   │   ├── base.py         # 基礎 Normalizer
│   │   │   ├── crypto.py       # 加密貨幣
│   │   │   ├── equity.py       # 股票 / 指數
│   │   │   └── fx.py           # 外匯
│   │   └── dq/                 # 資料品質檢查
│   │       └── validators.py
│   │
│   └── utils/                  # 工具函式
│       ├── uuid7.py
│       └── datetime_utils.py
│
├── scripts/
│   ├── seed_data.py            # 資料種子腳本
│   ├── cleanup_raw.py          # Raw 清理腳本
│   └── sample_ingest_payload.json
│
├── tests/                      # 測試
├── migrations/                 # Alembic 遷移
├── docs/                       # 文件
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
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
curl http://localhost:8000/health

# API 文件
open http://localhost:8000/docs
```

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

### Source API（資料攝取）

需在 Header 帶入 `X-API-Key`。

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/v1/source/ingest` | 接收 raw payload |
| GET | `/api/v1/source/runs/{run_id}` | 查詢批次狀態 |
| GET | `/api/v1/source/datasets` | 查詢可用資料集 |

#### Ingest 請求範例

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@scripts/sample_ingest_payload.json"
```

#### 請求格式

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

### Serve API（資料查詢）

認證可選（由 `SERVE_REQUIRE_AUTH` 控制）。

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/v1/serve/instruments` | 查詢標的清單 |
| GET | `/api/v1/serve/instruments/{id}` | 查詢單一標的 |
| GET | `/api/v1/serve/eod` | 查詢日K資料 |
| GET | `/api/v1/serve/eod/{instrument_id}` | 查詢單一標的日K |
| GET | `/api/v1/serve/calendar` | 查詢交易日曆 |

#### 查詢範例

```bash
# 查詢加密貨幣標的
curl "http://localhost:8000/api/v1/serve/instruments?market=CRYPTO"

# 查詢 BTC/ETH 日K
curl "http://localhost:8000/api/v1/serve/eod?market=CRYPTO&symbols=BTC,ETH&start_date=2026-01-01&end_date=2026-01-16"
```

---

## 資料模型

### Canonical Layer（長期儲存）

| 資料表 | 說明 |
|--------|------|
| `instruments` | 標的主檔（股票/指數/加密貨幣/外匯） |
| `instrument_identifiers` | 代碼映射（Bloomberg/ISIN/CUSIP） |
| `trading_calendar` | 交易日曆 |
| `market_data_eod` | 日K資料（OHLCV） |
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

### 測試環境注意事項

- 測試使用 `findb_test` 資料庫
- `SOURCE_API_KEYS` 必須設定否則認證測試會失敗
- 測試會自動建立/銷毀資料表

---

## 部署

### Docker Compose（推薦）

```bash
# 啟動
docker-compose up -d --build

# 查看日誌
docker-compose logs -f app

# 停止
docker-compose down
```

### 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `DATABASE_URL` | PostgreSQL 連線字串 | `postgresql+asyncpg://findb:findb@localhost:5435/findb` |
| `SOURCE_API_KEYS` | Source API 金鑰（逗號分隔） | - |
| `SERVE_API_KEYS` | Serve API 金鑰（逗號分隔） | - |
| `SERVE_REQUIRE_AUTH` | Serve API 是否需要認證 | `false` |
| `RAW_RETENTION_DAYS` | Raw 資料保留天數 | `14` |
| `DEBUG` | 除錯模式 | `false` |

### Raw 資料清理

設定每日排程執行：

```bash
docker-compose exec app python /app/scripts/cleanup_raw.py
```

---

## 相關文件

- [開發計劃](plans/normalize_serve_development_plan.md)
- [技術規格](spec.md)
- [產品路線圖](roadmap.md)
- [API 測試流程](docs/api_test_flow.md)

---

## 授權

Private - FinDB Team
