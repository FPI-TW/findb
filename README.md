# FinDB - Financial Database

Normalize + Serve 層後端服務，用於金融資料的標準化、儲存與查詢。

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

### 已完成

- Source / Normalize / Serve / Admin 四條主路徑已串接完成
- Source API 已支援標準 ingest、run status、rerun、dataset registry 查詢
- Bloomberg direct ingest 已支援 `crypto`、`fx`、`wtx`、`macro`、`usstock`、`hkchina`
- Serve API 已支援 instruments、EOD、corporate actions、macro、futures、calendar 查詢
- Admin API 已支援 DQ issue 查詢/解決、EOD patch、更正紀錄、raw payload 查詢、bulk rerun、instrument cache 維護
- 測試已涵蓋 Source / Serve / Admin / normalize / direct ingest / end-to-end 主流程

### 目前可用資料範圍

| 類型               | 現況                                                           |
| ------------------ | -------------------------------------------------------------- |
| `CRYPTO`           | EOD、指數 EOD、Bloomberg direct ingest                         |
| `US`               | Equity / Index EOD、corporate actions、Bloomberg direct ingest |
| `FX`               | EOD、Bloomberg direct ingest                                   |
| `TW` / `HK` / `CN` | 區域股票與指數正規化已實作，港中 direct ingest 已串接          |
| `MACRO`            | Observation 與 Bloomberg direct ingest 已實作                  |
| `WTX`              | Continuous futures 與 Bloomberg direct ingest 已實作           |

### 下一階段重點

- 將 normalize 從 FastAPI `BackgroundTasks` 拆到獨立 worker / queue
- 補齊 migration、bulk write、索引與大批量效能優化
- 持續擴充樣本資料、壓測與維運文件

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
| 套件管理 | uv                      |
| 容器化   | Docker / Docker Compose |

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
│       └── deploy.yml          # CI/CD：test → build → deploy
│
├── scripts/
│   ├── seed_data.py            # 資料種子腳本
│   ├── cleanup_raw.py          # Raw 清理腳本
│   ├── generate_instrument_cache.py  # 產生標的與宏觀序列查詢快取
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
│   ├── normalize_serve_development_plan.md
│   ├── spec.md                 # 技術規格
│   └── roadmap.md              # 產品路線圖
│
├── docker-compose.yml          # 本機開發環境
├── docker-compose.prod.yml     # 生產環境（AWS EC2）
├── Dockerfile
├── pyproject.toml
└── .env.example
```

---

## 快速開始

### 前置需求

- Python 3.13+
- Docker & Docker Compose
- uv

### 1. 複製環境設定

```bash
cp .env.example .env
```

編輯 `.env` 設定 API Keys：

```env
SOURCE_API_KEYS=your-source-key
SERVE_API_KEYS=your-serve-key
ADMIN_API_KEYS=your-admin-key
SERVE_REQUIRE_AUTH=false
```

> **注意**：`docker-compose.yml` 使用 `${SOURCE_API_KEYS:-dev-source-key}` 語法，
> 若 `.env` 未設定則預設使用 `dev-source-key`。本機測試可直接使用預設值。
> `.env.example` 已提供本機 `SOURCE_ALLOWLIST_CIDRS` 預設值（localhost + 私網段）；
> 若改成自訂來源，請依實際來源 IP/CIDR 調整。

### 2. 開發命令（固定主命令）

以下為標準入口（macOS / Linux / Windows 共通）：

```bash
uv run python scripts/dev.py up-db
uv run python scripts/dev.py up-server
uv run python scripts/dev.py up
uv run python scripts/dev.py test-db
uv run python scripts/dev.py down
uv run python scripts/dev.py partial-dump-validate
uv run python scripts/dev.py partial-dump-run --dry-run
uv run python scripts/dev.py seed-upsert
uv run python scripts/dev.py seed-upsert --truncate
uv run python scripts/dev.py seed-upsert --artifact-dir seed/partial_dump/<seed_package_name>
```

雙平台 wrapper：

```bash
# macOS / Linux
make up-db
make up-server
make up
make test-db
make down
make partial-dump-validate
make partial-dump-run
make seed-upsert
make seed-upsert-truncate
```

```powershell
# Windows PowerShell
.\scripts\dev.ps1 up-db
.\scripts\dev.ps1 up-server
.\scripts\dev.ps1 up
.\scripts\dev.ps1 test-db
.\scripts\dev.ps1 down
.\scripts\dev.ps1 partial-dump-validate
.\scripts\dev.ps1 partial-dump-run --dry-run
.\scripts\dev.ps1 seed-upsert
.\scripts\dev.ps1 seed-upsert --truncate
.\scripts\dev.ps1 seed-upsert --artifact-dir seed/partial_dump/<seed_package_name>
```

### 3. 常用開發流程

```bash
# 安裝依賴
uv sync

# 只啟動 DB
uv run python scripts/dev.py up-db

# 只啟動本機 server（連線本機 5435 DB）
uv run python scripts/dev.py up-server
```

```bash
# 同時啟動 app image + db image（兩容器）
uv run python scripts/dev.py up

# 初始化資料（以 /seed 內 seed 包匯入可重現資料）
uv run python scripts/dev.py seed-upsert

# 跑包含 DB 的測試
uv run python scripts/dev.py test-db
```

可選工具服務（不預設啟動）：

```bash
docker compose --profile tools up -d pgadmin raw-cleanup
```

### 4. Partial Dump

```bash
# 1) 準備來源 DB 連線（唯讀）
export FINDB_REMOTE_DATABASE_URL='postgresql+asyncpg://user:pass@host:5432/dbname'

# 2) 驗證 partial_dump.yaml 契約
uv run python scripts/dev.py partial-dump-validate

# 3) 先看規劃結果（不匯出資料）
uv run python scripts/dev.py partial-dump-run --dry-run

# 4) 實際匯出（CSV + manifest）
uv run python scripts/dev.py partial-dump-run

# 5) 對啟動中的本地 DB 套用初始資料（UPSERT）
uv run python scripts/dev.py seed-upsert

# 6) 指定 seed 包版本（避免拿到最新包）
uv run python scripts/dev.py seed-upsert --artifact-dir seed/partial_dump/prod_partial_1y_3inst_all_tables_20260422T024418Z

# 7) 大改版時重建本地資料（清空 + 刪除舊表後再 UPSERT）
uv run python scripts/dev.py seed-upsert --truncate
```

- 設定檔：`configs/partial_dump.yaml`
- 目前預設只匯出 `public` schema（不含 `raw`）
- 預設抽樣：每市場 3 檔標的、最近 1 年（依 `selection` 調整）
- `seed-upsert` 預設會選 `seed/partial_dump/` 最新一包 seed 包（建議進版控），並依主鍵做 upsert 到 `DATABASE_URL`
- `seed-upsert --artifact-dir ...` 可鎖定特定 seed 包版本（參數名沿用舊稱），避免「最新包」隨時間變動
- `seed-upsert --truncate` 會先刪除與 seed 包同 schema 中「不在 seed 包內」的舊表（保留 `public.alembic_version`），再 `TRUNCATE ... RESTART IDENTITY CASCADE` 後匯入，適合本地大改版重建

<a id="git-lfs"></a>

### 4.1 Git LFS：seed CSV 管理

本專案使用 Git LFS 管理 partial dump seed 包內的 CSV，避免大型資料檔直接進入 Git 物件庫。

目前 `.gitattributes` 規則：

```gitattributes
seed/**/*.csv filter=lfs diff=lfs merge=lfs -text
```

也就是說，`seed/partial_dump/<seed_package_name>/tables/*.csv` 會由 Git LFS 追蹤；`manifest.json`、`schema.sql`、`load.sql` 仍是一般 Git 文字檔。

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
git check-attr filter diff merge text -- seed/partial_dump/<seed_package_name>/tables/public.market_data_eod.csv
```

正常情況下，`git check-attr` 會顯示 `filter: lfs`、`diff: lfs`、`merge: lfs`、`text: unset`。

#### 新增或更新 seed 包

```bash
# 1) 產生 partial dump seed 包
uv run python scripts/dev.py partial-dump-run

# 2) 確認 CSV 會被 LFS 規則接住
git lfs track
git check-attr filter -- seed/partial_dump/<seed_package_name>/tables/public.market_data_eod.csv

# 3) 檢查 LFS 追蹤清單
git lfs ls-files

# 4) staging：CSV 會以 LFS pointer 進 Git，實體內容由 LFS 管理
git add .gitattributes seed/partial_dump/<seed_package_name>

# 5) commit
git commit -m "Add partial dump seed package"
```

如果未來需要把其他大型 seed 檔案類型納入 LFS，先更新追蹤規則，再加入檔案：

```bash
git lfs track "seed/**/*.parquet"
git add .gitattributes seed/
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

## 靜態標的與宏觀序列查詢頁

先產生快取檔：

```bash
uv run python scripts/generate_instrument_cache.py
```

可用環境變數：

```bash
FINDB_BASE_URL=http://localhost:8080
FINDB_SERVE_API_KEY=your-serve-key
```

產生完成後，可透過下列網址開啟查詢頁：

```text
http://localhost:8080/static/instrument-lookup.html
```

排程範例：

```bash
0 6 * * * cd /app && python scripts/generate_instrument_cache.py
```

---

## API 文件

> 完整的端點規格、請求/回應格式、Python 範例與常見問題，請參閱
> **[API 使用教學](docs/api_usage_guide.md)**。

### Source API（資料攝取）

需在 Header 帶入 `X-API-Key`。

| 方法 | 路徑                                         | 說明                                      |
| ---- | -------------------------------------------- | ----------------------------------------- |
| POST | `/api/v1/source/ingest/crypto`               | 接收 CRYPTO 市場 raw payload              |
| POST | `/api/v1/source/ingest/us`                   | 接收 US 市場 raw payload                  |
| POST | `/api/v1/source/ingest/fx`                   | 接收 FX 市場 raw payload                  |
| POST | `/api/v1/source/ingest/macro`                | 接收 MACRO 市場 raw payload               |
| POST | `/api/v1/source/ingest/wtx`                  | 接收 WTX 市場 raw payload                 |
| POST | `/api/v1/source/ingest/global`               | 接收 GLOBAL 市場 raw payload              |
| POST | `/api/v1/source/ingest/tw`                   | 接收 TW 市場 raw payload                  |
| POST | `/api/v1/source/ingest/hk`                   | 接收 HK 市場 raw payload                  |
| POST | `/api/v1/source/ingest/cn`                   | 接收 CN 市場 raw payload                  |
| POST | `/api/v1/source/ingest/crypto/direct`        | Bloomberg 加密貨幣直接格式                |
| POST | `/api/v1/source/ingest/fx/direct`            | Bloomberg 外匯直接格式                    |
| POST | `/api/v1/source/ingest/wtx/direct`           | Bloomberg WTX 期貨直接格式                |
| POST | `/api/v1/source/ingest/usstock/direct`       | Bloomberg 美股直接格式                    |
| POST | `/api/v1/source/ingest/hkchina/direct`       | Bloomberg 港中混合直接格式（股票 + 指數） |
| POST | `/api/v1/source/ingest/hkchina-index/direct` | Bloomberg 港中指數直接格式（相容舊流程）  |
| POST | `/api/v1/source/ingest/macro/direct`         | Bloomberg 宏觀直接格式                    |
| GET  | `/api/v1/source/runs/{run_id}`               | 查詢批次狀態                              |
| POST | `/api/v1/source/runs/{run_id}/rerun`         | 以原始 payload 重新執行正規化             |
| GET  | `/api/v1/source/datasets`                    | 查詢可用資料集                            |

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

| 方法 | 路徑                                               | 說明                                                |
| ---- | -------------------------------------------------- | --------------------------------------------------- |
| GET  | `/api/v1/serve/instruments`                        | 查詢標的清單（支援 market/asset_class/symbol 篩選） |
| GET  | `/api/v1/serve/instruments/{id}`                   | 查詢單一標的                                        |
| GET  | `/api/v1/serve/eod`                                | 查詢日K 資料（支援 market/symbols/日期範圍篩選）    |
| GET  | `/api/v1/serve/eod/{instrument_id}`                | 查詢單一標的日K                                     |
| GET  | `/api/v1/serve/corporate-actions`                  | 查詢公司行為（除權息）                              |
| GET  | `/api/v1/serve/corporate-actions/{instrument_id}`  | 查詢單一標的公司行為                                |
| GET  | `/api/v1/serve/macro/series`                       | 查詢宏觀指標序列                                    |
| GET  | `/api/v1/serve/macro/observations`                 | 查詢宏觀觀測值                                      |
| GET  | `/api/v1/serve/macro/observations/{series_id}`     | 查詢特定序列觀測值                                  |
| GET  | `/api/v1/serve/futures/contracts`                  | 查詢期貨合約                                        |
| GET  | `/api/v1/serve/futures/continuous`                 | 查詢連續期貨日K                                     |
| GET  | `/api/v1/serve/futures/continuous/{instrument_id}` | 查詢特定標的連續期貨日K                             |
| GET  | `/api/v1/serve/calendar`                           | 查詢交易日曆（market 必填）                         |

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

### Admin API（資料修正與快取維護）

Admin API 必須帶 `X-API-Key`，並使用 `ADMIN_API_KEYS` 中配置的值。

| 方法  | 路徑                                                   | 說明                                    |
| ----- | ------------------------------------------------------ | --------------------------------------- |
| GET   | `/api/v1/admin/dq-issues`                              | 查詢 DQ issue                           |
| PATCH | `/api/v1/admin/eod/{instrument_id}/{trade_date}`       | 修正 EOD 欄位並寫入 audit log           |
| PATCH | `/api/v1/admin/dq-issues/{issue_id}/resolve`           | 標記 DQ issue 已解決                    |
| GET   | `/api/v1/admin/raw-payloads`                           | 查詢 raw payload 清單                   |
| GET   | `/api/v1/admin/raw-payloads/{run_id}`                  | 查詢指定 run 的 raw payload             |
| GET   | `/api/v1/admin/corrections`                            | 查詢修正紀錄                            |
| POST  | `/api/v1/admin/runs/bulk-rerun`                        | 批次重跑既有 runs                       |
| GET   | `/api/v1/admin/instrument-cache`                       | 讀取 `app/static/data/instruments.json` |
| PUT   | `/api/v1/admin/instrument-cache`                       | 全量覆蓋 instrument cache               |
| PATCH | `/api/v1/admin/instrument-cache/items/{instrument_id}` | 更新單一 instrument cache 項目          |

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

| 層級       | 認證                         | Header                         |
| ---------- | ---------------------------- | ------------------------------ |
| Source API | **必要**                     | `X-API-Key: {SOURCE_API_KEYS}` |
| Serve API  | 可選（`SERVE_REQUIRE_AUTH`） | `X-API-Key: {SERVE_API_KEYS}`  |
| Admin API  | **必要**                     | `X-API-Key: {ADMIN_API_KEYS}`  |

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
uv run black app tests scripts

# Lint
uv run ruff check .

# 型別檢查
uv run mypy app
```

### 新增 Normalizer

1. 在 `app/services/normalize/` 建立新檔案
2. 繼承 `BaseNormalizer`，實作 `map_fields()`
3. 在 `app/services/normalize/__init__.py` 匯出
4. 在 `app/services/ingestion.py` 的 `NORMALIZER_MAP` 加入對應
5. 在 `scripts/seed_data.py` 加入 dataset 設定

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
uv run python scripts/dev.py test-db

# 執行所有測試
uv run pytest

# 執行單一檔案
uv run pytest tests/test_source_api.py

# 執行特定測試
uv run pytest tests/test_source_api.py::TestSourceAPI::test_ingest_without_api_key

# 依關鍵字執行
uv run pytest -k "crypto"

# 顯示覆蓋率
uv run pytest --cov=app
```

### Docker 容器內測試

```bash
# 使用容器內的 test DB
docker compose exec app bash -c \
  "TEST_DATABASE_URL=postgresql+asyncpg://findb:findb@db:5432/findb_test pytest"
```

### 測試環境注意事項

- 測試使用 `findb_test` 資料庫（可透過 `TEST_DATABASE_URL` 環境變數覆蓋）
- `SOURCE_API_KEYS` 必須設定否則認證測試會失敗
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
# 安裝 hooks（一次）
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

目前規則：

- `pre-commit`：執行 `black` 自動格式化。
- `pre-push`：執行含 DB 的測試流程（`uv run python scripts/dev.py test-db`）。

## Migration

- 操作流程與 baseline/stamp 策略請見：[docs/migration_workflow.md](docs/migration_workflow.md)
- 常用命令：`uv run alembic current`、`uv run alembic revision --autogenerate -m \"...\"`、`uv run alembic upgrade head`
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
  └── deploy   → SSH → docker compose pull & up
```

- **Image Registry**：GitHub Container Registry（ghcr.io）
- **資料庫**：AWS Aurora/RDS（EC2 不跑 PostgreSQL 容器）
- **部署方式**：SSH + `docker compose -f docker-compose.prod.yml`

#### 首次 EC2 初始化（一次性）

```bash
# 1. 安裝 Docker
sudo bash scripts/setup_ec2.sh

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
| `SOURCE_API_KEYS`     | Source API 金鑰（逗號分隔）                                              |
| `ADMIN_API_KEYS`      | Admin API 金鑰（逗號分隔）                                               |
| `SERVE_API_KEYS`      | Serve API 金鑰（`SERVE_REQUIRE_AUTH=true` 時必填）                       |
| `FINDB_SERVE_API_KEY` | 產生靜態查詢快取使用的 Serve API key（`SERVE_REQUIRE_AUTH=true` 時必填） |

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
| `SOURCE_ALLOWLIST_CIDRS`     | 生產允許名單，例如 `10.0.0.0/8,203.0.113.50/32`                              |
| `SOURCE_TRUST_PROXY_HEADERS` | 是否信任反向代理 header，例如 `false`                                        |
| `SERVE_REQUIRE_AUTH`         | Serve API 是否需要認證，例如 `false`                                         |
| `RATE_LIMIT_REQUESTS`        | `100`                                                                        |
| `RATE_LIMIT_WINDOW`          | `60`                                                                         |
| `RAW_RETENTION_ENABLED`      | 是否啟用 raw 清理，例如 `false`                                              |
| `RAW_RETENTION_DAYS`         | `14`                                                                         |
| `FINDB_BASE_URL`             | 部署後服務 URL；若 cache 在 app container 內產生可用 `http://localhost:8080` |
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
| `DEBUG`                      | 除錯模式（跳過允許名單強制檢查） | `false`                                                        |
| `SOURCE_API_KEYS`            | Source API 金鑰（逗號分隔）      | `docker compose` 開發環境預設 `dev-source-key`                 |
| `ADMIN_API_KEYS`             | Admin API 金鑰（逗號分隔）       | （空）                                                         |
| `SOURCE_ALLOWLIST_CIDRS`     | IP 允許名單（CIDR，逗號分隔）    | `127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` |
| `SOURCE_TRUST_PROXY_HEADERS` | 是否信任 X-Forwarded-For         | `false`                                                        |
| `SERVE_API_KEYS`             | Serve API 金鑰（逗號分隔）       | （空）                                                         |
| `SERVE_REQUIRE_AUTH`         | Serve API 是否需要認證           | `false`                                                        |
| `RATE_LIMIT_REQUESTS`        | 限流上限（每 window 內的請求數） | `100`                                                          |
| `RATE_LIMIT_WINDOW`          | 限流時間窗口（秒）               | `60`                                                           |
| `RAW_RETENTION_ENABLED`      | 是否啟用 Raw 過期清理            | `false`                                                        |
| `RAW_RETENTION_DAYS`         | Raw 資料保留天數                 | `14`                                                           |

> **注意**：本機 `docker-compose.yml` 仍可讀取 `.env` 或 shell 環境變數；生產 `docker-compose.prod.yml` 只接受 GitHub Actions 在部署時傳入的環境變數。
> 目前預設不會自動刪除 raw payload；正式上線時再將 `RAW_RETENTION_ENABLED=true` 啟用即可。

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
- [技術規格](plans/spec.md)
- [產品路線圖](plans/roadmap.md)
- [開發計劃](plans/normalize_serve_development_plan.md)

---

## 授權

Private - FinDB Team
