# FinDB API 使用教學

> **版本**: 0.1.0 | **最後更新**: 2026-04-21

本文件說明如何使用 FinDB 的 Source API（資料寫入）、Serve API（資料查詢）與 Admin API（資料修正）。
涵蓋認證機制、所有端點規格、請求/回應格式、錯誤處理與完整範例。

---

## 目錄

- [系統架構概述](#系統架構概述)
- [目前實作範圍](#目前實作範圍)
- [快速開始](#快速開始)
- [認證機制](#認證機制)
- [通用格式](#通用格式)
- [Source API（資料寫入）](#source-api資料寫入)
  - [標準攝取端點](#標準攝取端點)
  - [Direct 格式攝取端點](#direct-格式攝取端點)
  - [批次管理端點](#批次管理端點)
- [Serve API（資料查詢）](#serve-api資料查詢)
  - [標的查詢](#標的查詢)
  - [日K 資料](#日k-資料)
  - [公司行為](#公司行為)
  - [宏觀經濟指標](#宏觀經濟指標)
  - [期貨](#期貨)
  - [交易日曆](#交易日曆)
- [Admin API（資料修正）](#admin-api資料修正)
  - [管理 Instrument Cache](#管理-instrument-cache)
  - [查詢 DQ Issues](#查詢-dq-issues)
  - [修正日K 資料](#修正日k-資料)
  - [標記 DQ Issue 已解決](#標記-dq-issue-已解決)
  - [查詢 Raw Payload](#查詢-raw-payload)
  - [查詢修正紀錄](#查詢修正紀錄)
  - [批次重跑既有 Run](#批次重跑既有-run)
- [分頁機制](#分頁機制)
- [錯誤代碼一覽](#錯誤代碼一覽)
- [Python 範例](#python-範例)
- [常見問題](#常見問題)

---

## 系統架構概述

FinDB 採用三層解耦架構：

```
Fetch Layer（外部設備）
    │
    │  POST /api/v1/source/ingest/{market}
    ▼
Source API ──▶ Normalize ──▶ Canonical DB
                                  │
                      ┌───────────┤
                      │           │
                      ▼           ▼
                 Admin API    Serve API ──▶ 消費者（報告/AI/圖表）
               （資料修正）
```

| 層級           | 角色     | 說明                                          |
| -------------- | -------- | --------------------------------------------- |
| **Source API** | 寫入路徑 | 接收原始 payload、去重、觸發正規化            |
| **Serve API**  | 讀取路徑 | 唯讀查詢正規化後的 Canonical 資料             |
| **Admin API**  | 修正路徑 | 人工修正 Canonical 資料，寫入不可變 audit log |

> 完整的 ingest 內部五階段流程（守門點、Raw + Run 落地、背景正規化、維運鉤子）可參考視覺化頁面 `ingestion_workflow.html`。

---

## 目前實作範圍

以下內容是 2026-04-21 這個版本的實際可用範圍：

| 區塊          | 現況                                                                                   |
| ------------- | -------------------------------------------------------------------------------------- |
| Source API    | 標準 ingest、direct ingest、run status、rerun、dataset list 已可用                     |
| Direct ingest | `crypto`、`fx`、`wtx`、`macro`、`usstock`、`hkchina`、`twstock` 已可用                 |
| Serve API     | instruments、EOD、corporate actions、macro、futures、bonds、calendar 已可用            |
| Admin API     | DQ issue、EOD patch、corrections、raw payload、bulk rerun、instrument cache 管理已可用 |
| 區域市場      | `TW` / `HK` / `CN` 的 equity / index normalizer 已實作並串接到主流程                   |

> Source API 會先把 raw、run、job 與 outbox 原子提交，再回 `202 Accepted`。RabbitMQ 暫時中斷時仍可接受請求，dispatcher 會在恢復後補送。

---

## 快速開始

### 1. 啟動服務

```bash
# 複製環境設定
cp .env.example .env

# 編輯 .env，設定 API Key
# SOURCE_API_KEY=your-source-key
# ADMIN_API_KEY=your-admin-key
# DEBUG=true

# 啟動本機 DB、套用 migration、匯入 seed 包
uv run python scripts/dev.py up-db
uv run alembic upgrade head
uv run python scripts/dev.py seed-upsert --truncate

# 啟動本機 FastAPI 服務
uv run python scripts/dev.py up-server
```

> 生產環境由 nginx 使用 `SOURCE_ALLOWLIST_CIDRS` 限制 `/api/v1/source/*`；本機直接跑 FastAPI 時不執行 IP 允許名單。
> `docker-compose.yml` 目前未把 `ADMIN_API_KEYS` 傳入 app container；若要用 Docker app 容器測 Admin API，需先補上對應環境變數映射，或改用 `uv run python scripts/dev.py up-server` 啟動本機服務。

### 2. 驗證服務

```bash
curl http://localhost:8080/health
```

回應範例：

```json
{
  "status": "healthy",
  "version": "0.1.0"
}
```

### 3. 查看互動式文件

瀏覽器開啟 [http://localhost:8080/docs](http://localhost:8080/docs)（Swagger UI）。

---

## 認證機制

### Source API（必要）

所有 Source API 端點**必須**在 HTTP Header 帶入 API Key：

```
X-API-Key: your-source-key
```

未帶入或無效金鑰會收到 `401` 或 `403` 錯誤。

### Serve API（可選）

Serve API 的認證由環境變數 `SERVE_REQUIRE_AUTH` 控制：

| 設定值          | 行為                                              |
| --------------- | ------------------------------------------------- |
| `false`（預設） | 不需要認證，任何人皆可查詢                        |
| `true`          | 必須帶入 `X-API-Key`，金鑰優先查 DB `api_key` 表，`SERVE_API_KEYS` 僅作過渡 fallback |

啟用 Serve 認證時，使用方式與 Source API 相同：

```
X-API-Key: your-serve-key
```

Serve API key 由 Admin API 簽發，明文只在建立時回傳一次：

```bash
curl -X POST "http://localhost:8080/api/v1/admin/api-keys" \
  -H "X-API-Key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"owner":"llm-client","tier":"llm","scopes":["serve"],"rate_limit_requests":100,"rate_limit_window":60,"page_size_limit":1000}'
```

> **生產環境 nginx Serve key 注入**：`/instrument-lookup` 等同源靜態頁不會把 Serve API
> key 嵌入瀏覽器；生產 nginx 會以 `Referer` regex 比對後注入 `X-API-Key`。設定
> 由 `scripts/render_nginx_serve_key.py` 在 deploy 時根據 `SERVE_API_KEYS` 的
> **第一個** key 渲染為 `infra/nginx/serve-key.conf`。Phase 3 後這是過渡機制：
> 該 key 也應透過 Admin API 建入 DB，待部署確認後再移除 env fallback。

### Admin API（必要）

所有 Admin API 端點**永遠強制**認證，沒有任何 bypass 或 DEBUG 模式例外：

```
X-API-Key: your-admin-key
```

| 狀況                    | 結果  |
| ----------------------- | ----- |
| 未帶入 `X-API-Key`      | `401` |
| API Key 無效            | `403` |
| `ADMIN_API_KEY` 未配置 | `500` |

> **注意**：Admin API 不設 IP 允許名單，金鑰是唯一保護機制，請妥善保管並與 Source/Serve API Key 分開管理。

### 安全機制

| 機制            | 說明                                                          |
| --------------- | ------------------------------------------------------------- |
| **IP 允許名單** | 生產環境 nginx 使用 `SOURCE_ALLOWLIST_CIDRS` 限制 `/api/v1/source/*` |
| **限流**        | 預設每個 API Key + IP 組合，每 60 秒最多 100 次請求           |
| **Proxy 支援**  | nginx 會覆寫 `X-Real-IP` 與 `X-Forwarded-For` 為實際來源 IP |
| **Serve key 注入** | 生產 nginx 對 Referer 匹配 `/instrument-lookup` 的 `/api/v1/serve/*` 請求自動注入 `X-API-Key`；外部 caller 仍 passthrough |

---

## 通用格式

### 基礎 URL

```
http://localhost:8080
```

### 時間格式

所有時間戳記使用 **UTC ISO 8601** 格式：

```
2026-01-16T14:49:14Z
```

### 日期格式

日期欄位使用 `YYYY-MM-DD`：

```
2026-01-16
```

### ID 格式

所有 ID 使用 **UUID v7**：

```
019462f0-7c00-7000-8000-000000000001
```

---

## Source API（資料寫入）

**前綴**: `/api/v1/source`
**認證**: 必要（`X-API-Key`）

### Versioned canonical ingest（新格式）

所有新 fetch client 使用單一入口：

```
POST /api/v1/source/ingest
```

Fetch layer 必須先把 Bloomberg、FinLab 等 provider 原始欄位轉為 FinDB contract。目前支援：

Dataset config 的 `schema_enforcement="audit"` 是 legacy feed 的遷移旗標；呼叫 canonical endpoint 時仍會完整強制驗證 schema id/version 與所有欄位。Contract 的 `defaults.market` 與 `defaults.asset_class` 為必填，且必須分別等於 dataset registry 的 `market` 與 `asset_class`，避免資料被寫入錯誤 canonical scope。

| `schema_id` | `schema_version` | 用途 |
| --- | --- | --- |
| `market_eod` | `1` | 股票、ETF、指數、crypto、FX 日 OHLCV |
| `futures_continuous_eod` | `1` | 期貨連續序列日 OHLCV |

#### Machine-readable request-body contract

Fetch adapter 測試可用相同 Source API key 讀取指定版本的 Draft 2020-12 JSON Schema：

```http
GET /api/v1/source/contracts/market_eod/versions/1
X-API-Key: <SOURCE_API_KEY>
```

另一個已發布版本為
`/api/v1/source/contracts/futures_continuous_eod/versions/1`。回應的 `$id` 固定為
`urn:findb:ingress-contract:{schema_id}:v{schema_version}`；已發布版本的 schema 不會原地改變
不相容語意，breaking change 必須新增 version。這份 artifact 的範圍是 versioned request-body
shape、body normalization 與 body semantic validation，不是完整 API acceptance contract。
`x-findb-semantic-rules` 列出 JSON Schema
無法單獨表達的跨欄位／跨列規則，adapter fixture tests 除了執行 JSON Schema validator，
也必須依每筆 rule 的穩定 `id`、`scope`、`parameters` 驗證這些規則。這些 rules 包含
timezone-aware `fetched_at`、batch sequence/coverage 關聯、backfill 日期、OHLC bounds、coverage 包含 row 日期、declared
count、delivery natural-key uniqueness、動態大小限制與 currency resolution。`context_dependencies`
為空的規則只依 request；`kind=runtime_setting` 需要測試環境注入
`SOURCE_MAX_DATA_ITEMS`／`SOURCE_MAX_PAYLOAD_BYTES`，`kind=dataset_context` 則需要對應 dataset
的 `defaults.currency`。`x-findb-transformations` 則明確列出 model validation 前執行的字串
trim paths。`schema_id` 與 `schema_version` 是 pre-model registry dispatch discriminators，不在
trim paths；`x-findb-contract-scope.dispatch_discriminators` 將其標示為 exact、無 dispatch 前
normalization。Fetch adapter 不得為這兩個欄位加空白或用字串代替 integer version。

`x-findb-contract-scope` 以 `covers` 與 `additional_acceptance_boundaries` 說明範圍；其中
`sufficient_for_api_acceptance=false`。通過 JSON Schema、transformations 與 semantic rules 是
API 接受請求的必要條件，但不是充分條件。認證與 credential DB lookup、credential/client-IP
rate limit、client-IP availability、source
binding、dataset allowlist/existence/active/declaration/scope/accepted version、idempotency collision
及 infrastructure state 仍由動態 application boundaries 決定，不應嘗試編碼成 JSON Schema。
各 boundary 已有的 HTTP status、固定 code 與 attempt semantics 會出現在 scope extension；完整
HTTP 行為以本節後方的錯誤表與 canonical ingest／attempt endpoint 說明為準。

`request.body.max_bytes` 是 middleware 的 pre-attempt transport gate：已知 Content-Length 超限時
回一般 `413 detail`，不建立 attempt。`payload.serialized.max_bytes` 則描述進入 contract validation
後對 compact JSON payload 的同一動態 byte limit。

Canonical ingest endpoint 刻意保留 raw request body handling，不讓 FastAPI/Pydantic 在 route
boundary 預先拒絕資料；因此通過認證與 rate-limit gate 後，即使 JSON 或 contract 無效，仍會先建立
durable `ingestion_attempt`。

請求範例：

```json
{
  "dataset_key": "tw_equity_eod",
  "schema_id": "market_eod",
  "schema_version": 1,
  "source": "finlab",
  "request_key": "finlab_tw_equity_eod_20260721_01",
  "idempotency_key": "finlab_tw_equity_eod_20260721",
  "fetched_at": "2026-07-21T08:00:00Z",
  "payload": {
    "batch": {
      "data_date": "2026-07-21",
      "delivery_mode": "full_snapshot",
      "declared_record_count": 1
    },
    "data": [
      {
        "symbol": "2330",
        "source_symbol": "2330 TT Equity",
        "trade_date": "2026-07-21",
        "name": "台積電",
        "currency": "TWD",
        "open": "1000",
        "high": "1020",
        "low": "995",
        "close": "1015",
        "volume": 32100000
      }
    ]
  }
}
```

成功回 `202 Accepted`：

```json
{
  "success": true,
  "attempt_id": "019f8abc-0000-7000-8000-000000000001",
  "run_id": "019f8abc-0000-7000-8000-000000000002",
  "status": "queued",
  "schema_id": "market_eod",
  "schema_version": 1,
  "message": "Data received, processing queued"
}
```

每次通過 API key 認證與 rate-limit gate 的呼叫都會建立獨立 `attempt_id`。重送相同 idempotency key 與內容時會建立 `duplicate` attempt，但回傳原有 `run_id`；相同 key 搭配不同 source、schema/version 或 payload 時回 `409`。缺少／無效 API key 或被 rate limit 的請求在 endpoint 前即被拒絕，因此不建立 attempt。

若資料庫在 attempt 建立前不可用，`503 DATABASE_UNAVAILABLE` 仍使用相同 error envelope，但 `attempt_id` 為 `null`；若 attempt 已 commit、後續 claim 才因資料庫／交易狀態失敗，503 會帶回已持久化的 `attempt_id`。非 DB 的 claim invariant 或程式錯誤回 `500 INTERNAL_ERROR`、不帶 `Retry-After`，但同樣保留該 attempt_id。Request 在處理期間會持有 attempt row claim，dispatcher 使用 `FOR UPDATE SKIP LOCKED`，因此不會回收仍活躍的 request；process 中斷會由 PostgreSQL 自動釋放 claim，dispatcher 再於 `INGESTION_ATTEMPT_STALE_SECONDS`（預設 300 秒）後分批將殘留的 `received` attempt 回收為 `aborted`，failure code 為 `ATTEMPT_INTERRUPTED`。每批上限由 `INGESTION_ATTEMPT_RECONCILE_BATCH_SIZE`（預設 100）控制，維護失敗只記錄 log，不會停止 outbox dispatch。

拒絕回應具有固定格式：

```json
{
  "success": false,
  "attempt_id": "019f8abc-0000-7000-8000-000000000003",
  "error": {
    "code": "INGRESS_SCHEMA_INVALID",
    "message": "payload.data.0.close: Field required"
  }
}
```

常見錯誤碼：

| HTTP | code | 說明 |
| --- | --- | --- |
| 400 | `DATASET_NOT_FOUND` | dataset 不存在 |
| 403 | `DATASET_ACCESS_DENIED` | source client 無權傳送 dataset |
| 403 | `SOURCE_IDENTITY_MISMATCH` | credential 綁定 provider 與 request source 不符 |
| 409 | `DATASET_INACTIVE` | dataset 已停用 |
| 409 | `DATASET_CONTRACT_NOT_CONFIGURED` | dataset 尚未宣告 versioned contract |
| 409 | `IDEMPOTENCY_PAYLOAD_MISMATCH` | idempotency key 已對應其他內容 |
| 422 | `INGRESS_SCHEMA_UNSUPPORTED` | schema id/version 未註冊 |
| 422 | `INGRESS_SCHEMA_INVALID` | JSON 或欄位不符合 contract |
| 422 | `INGRESS_SCHEMA_NOT_ALLOWED` | dataset 不接受指定 contract |
| 422 | `DECLARED_RECORD_COUNT_MISMATCH` | `payload.batch.declared_record_count` 不等於 `len(payload.data)` |
| 422 | `DUPLICATE_DELIVERY_KEY` | 同一 delivery 內出現重複的 `(symbol, trade_date)`；與 request/idempotency key 無關 |
| 422 | `CURRENCY_REQUIRED` | market row 與 dataset default 都無 currency，或 futures dataset 未提供 default currency |
| 503 | `DATABASE_UNAVAILABLE` | 資料庫暫時不可用；建立 attempt 前失敗時 `attempt_id=null` |

`futures_continuous_eod.v1` 的 `open_interest`、`active_contract_code` 與 `roll_adjustment` 目前會保留在 standardized raw payload，但尚未寫入 canonical table 或 Serve API；WTX fetch 切換前會另行完成欄位去向決策。

#### 查詢 attempt

```
GET /api/v1/source/attempts/{attempt_id}
```

只允許建立該 attempt 的 source client 查詢。Legacy `SOURCE_API_KEY` 只能查詢 legacy ownership scope（`source_client_id IS NULL`）的 attempt。

### 舊版標準攝取端點（相容路徑）

標準格式使用 `IngestRequest` 請求體，適用於 Fetch Layer 傳入的完整 payload。

#### 請求體格式（IngestRequest）

```json
{
  "dataset_key": "crypto_eod",
  "source": "bloomberg",
  "request_key": "bloomberg_crypto_20260116_144914",
  "idempotency_key": "bloomberg_crypto_20260116_144914",
  "payload": {
    "metadata": { ... },
    "data": [ ... ]
  },
  "fetched_at": "2026-01-16T14:49:14Z"
}
```

| 欄位              | 類型     | 必填 | 說明                                            |
| ----------------- | -------- | ---- | ----------------------------------------------- |
| `dataset_key`     | string   | 是   | 資料集識別碼（如 `crypto_eod`、`us_stock_eod`） |
| `source`          | string   | 是   | 資料來源（如 `bloomberg`）                      |
| `request_key`     | string   | 是   | 上游請求識別碼，用於追蹤                        |
| `idempotency_key` | string   | 是   | 去重鍵值，相同值不會重複處理                    |
| `payload`         | object   | 是   | 原始資料 payload（包含 `metadata` 與 `data`）   |
| `fetched_at`      | datetime | 是   | 資料擷取時間（UTC ISO 8601）                    |

#### 回應格式（IngestResponse）

```json
{
  "success": true,
  "run_id": "019462f0-7c00-7000-8000-000000000001",
  "status": "pending",
  "message": "Data received, processing queued"
}
```

| 欄位      | 類型   | 說明                                                    |
| --------- | ------ | ------------------------------------------------------- |
| `success` | bool   | 是否成功                                                |
| `run_id`  | UUID   | 攝取批次 ID，可用於查詢處理狀態                         |
| `status`  | string | 批次狀態（`queued`、`processing`、`retrying`、`completed`、`completed_with_errors`、`failed`） |
| `message` | string | 說明訊息                                                |

> 成功 durable commit 的 ingest 與 rerun 回 `202 Accepted`。相同 provider、dataset 與 `idempotency_key` 搭配相同 payload 會回既有 run；同 key 不同 payload 回 `409 Conflict`。

#### 端點一覽

| 方法 | 路徑             | 市場   | 說明         |
| ---- | ---------------- | ------ | ------------ |
| POST | `/ingest/crypto` | CRYPTO | 加密貨幣     |
| POST | `/ingest/us`     | US     | 美國股市     |
| POST | `/ingest/fx`     | FX     | 全球外匯     |
| POST | `/ingest/macro`  | MACRO  | 宏觀經濟     |
| POST | `/ingest/wtx`    | WTX    | 台灣加權指數 |
| POST | `/ingest/global` | GLOBAL | 全球市場     |
| POST | `/ingest/tw`     | TW     | 台灣市場     |
| POST | `/ingest/hk`     | HK     | 香港市場     |
| POST | `/ingest/cn`     | CN     | 中國市場     |

#### curl 範例

```bash
# 攝取加密貨幣資料
curl -X POST "http://localhost:8080/api/v1/source/ingest/crypto" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_key": "crypto_eod",
    "source": "bloomberg",
    "request_key": "bloomberg_crypto_20260116_144914",
    "idempotency_key": "bloomberg_crypto_20260116_144914",
    "payload": {
      "metadata": {
        "source": "Bloomberg API",
        "category": "Cryptocurrency",
        "query_time": "2026-01-16T14:49:14.910965",
        "total_records": 2
      },
      "data": [
        {
          "crypto_id": "bitcoin",
          "symbol": "BTC",
          "name": "Bitcoin",
          "ticker": "XBTUSD BGN Curncy",
          "price": { "last": 95709.01, "open": 95550.07, "high": 95825.34, "low": 95119.76 },
          "change": { "net": 158.81, "percent_1d": 0.1662 },
          "timestamp": { "query_time": "2026-01-16T14:49:14", "last_update": "2026-01-16" },
          "metadata": { "source": "Bloomberg", "data_type": "cryptocurrency" }
        }
      ]
    },
    "fetched_at": "2026-01-16T14:49:14Z"
  }'
```

```bash
# 從檔案攝取
curl -X POST "http://localhost:8080/api/v1/source/ingest/crypto" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@scripts/sample_ingest_payload.json"
```

---

### Direct 格式攝取端點

Direct 格式支援 Bloomberg 直接匯出的 `metadata + data` 結構，以及 FinLab 透過 tw-updater 推送的台股 / ETF / WTX 期貨格式。
系統會自動推斷 `dataset_key`、`source`、`idempotency_key` 等欄位。
Direct endpoint 可傳 `Idempotency-Key` header；未傳時才以內容 hash 產生 key。

#### 請求體格式（DirectIngestPayload）

```json
{
  "metadata": {
    "source": "bloomberg",
    "query_time": "2026-02-04T16:00:51Z"
  },
  "data": [
    {
      "ticker": "AAPL US Equity",
      "date": "2026-02-04",
      "open": 231.0,
      "high": 233.0,
      "low": 230.5,
      "close": 232.5,
      "volume": 45000000
    }
  ]
}
```

| 欄位       | 類型   | 必填 | 說明                                                                                                   |
| ---------- | ------ | ---- | ------------------------------------------------------------------------------------------------------ |
| `metadata` | object | 否   | 中繼資料；建議保留 `source` 與 `query_time`                                                            |
| `data`     | array  | 否   | 資料陣列；建議以原始扁平格式直接傳 `ticker`、`date`、`open`、`high`、`low`、`close`、`volume`、`value` |

> Direct ingest 預設以原始扁平格式為主，也相容巢狀 `price` / `timestamp` 格式。
> `metadata.query_time` 會作為這批資料的抓取時間；`metadata.category` 非必需。

#### 台股 / ETF FinLab 直接格式（TWStockDirectIngestPayload）

`/ingest/twstock/direct` 使用台股專用契約，接收 FinLab tw-updater 推送的 JSON。
依 `metadata.asset_class` 自動路由：

| `metadata.asset_class` | dataset_key | 用途 |
| --- | --- | --- |
| 空 / `STOCK` / `equity` / `stock` | `tw_equity_eod` | 台股個股 |
| `ETF` / `etf` | `tw_etf_eod` | 台股 ETF |

```json
{
  "metadata": {
    "name": "台積電",
    "source": "finlab",
    "asset_class": "STOCK",
    "file_name": "finlab_stocks_ohlcv.jsonl",
    "query_time": "2026-05-14T13:30:00Z"
  },
  "data": [
    {
      "symbol": "2330",
      "date": "2026-05-14",
      "open": 2250,
      "high": 2270,
      "low": 2230,
      "close": 2270,
      "total_volume": 39564699,
      "total_ticks": 83872
    }
  ]
}
```

| 欄位                            | 類型   | 必填 | 說明 |
| ------------------------------- | ------ | ---- | ---- |
| `metadata.symbol`               | string | 條件必填 | 若 `data[]` 每列都未提供 `symbol`，則必填 |
| `metadata.name`                 | string | 否   | 股票名稱 |
| `metadata.source`               | string | 否   | 預設 `finlab` |
| `metadata.asset_class`          | string | 否   | `STOCK` / `ETF`（大小寫不敏感），決定 dataset_key 路由 |
| `metadata.file_name`            | string | 否   | 原始檔名，僅供追蹤 |
| `data[].symbol`                 | string | 條件必填 | 若未提供 `metadata.symbol`，則每列都必須提供 |
| `data[].date`                   | string | 是   | 交易日期，`YYYY-MM-DD`。FinLab 對停牌/下市個股回傳的是該檔最後交易日，與 `metadata.query_time` 可能不同 |
| `data[].time`                   | string | 否   | 交易時間；只保留在 raw payload，不進 canonical |
| `data[].open` / `high` / `low` / `close` | number | 是 | OHLC |
| `data[].total_volume` / `volume` | int | 是 | 成交量；接受 `volume` 作為別名 |
| `data[].total_ticks`            | int | 是 | 成交筆數 |

> 過去 MultiCharts 來源的 `up_volume`、`down_volume`、`up_ticks`、`down_ticks` 已自 DB 與 API 移除；FinLab 來源無此資料。

#### 端點一覽

| 方法 | 路徑                           | 對應市場 | 自動 dataset_key              | 說明                                      |
| ---- | ------------------------------ | -------- | ----------------------------- | ----------------------------------------- |
| POST | `/ingest/crypto/direct`        | CRYPTO   | `crypto_bloomberg_eod`        | Bloomberg 加密貨幣直接格式                |
| POST | `/ingest/fx/direct`            | FX       | `fx_bloomberg_eod`            | Bloomberg 外匯直接格式                    |
| POST | `/ingest/wtx/direct`           | WTX      | `wtx_eod`                     | 台指期 OHLCV；依 `metadata.source` 切換 FinLab / Bloomberg normalizer |
| POST | `/ingest/usstock/direct`       | US       | `us_stock_eod`                | Bloomberg 美股直接格式                    |
| POST | `/ingest/hkchina/direct`       | GLOBAL   | `hkchina_mixed_eod`           | Bloomberg 港中混合直接格式（股票 + 指數） |
| POST | `/ingest/hkchina-index/direct` | GLOBAL   | `hkchina_index_eod`           | Bloomberg 港中指數直接格式（相容舊流程）  |
| POST | `/ingest/macro/direct`         | MACRO    | `macro_bloomberg_observation` | Bloomberg 宏觀直接格式                    |
| POST | `/ingest/twstock/direct`       | TW       | `tw_equity_eod` / `tw_etf_eod` | FinLab 台股 / ETF 直接格式（依 `metadata.asset_class` 自動路由） |

> `hkchina/direct` 會在同一批 payload 中同時處理港股/中資股票與港中指數，並依 ticker 自動落到 `HK` 或 `CN` 市場。
> 若上游仍維持舊的純 index 匯出流程，可繼續使用 `hkchina-index/direct`。

#### curl 範例

```bash
# 直接匯入美股資料
curl -X POST "http://localhost:8080/api/v1/source/ingest/usstock/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": {
      "source": "bloomberg",
      "query_time": "2026-02-04T16:00:51Z"
    },
    "data": [
      {
        "ticker": "AAPL US Equity",
        "date": "2026-02-04",
        "open": 231.00,
        "high": 233.00,
        "low": 230.50,
        "close": 232.50,
        "volume": 45000000
      }
    ]
  }'
```

```bash
# 從 Bloomberg 匯出檔直接匯入
curl -X POST "http://localhost:8080/api/v1/source/ingest/usstock/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@bloomberg_usstock_20260204_160051_original.json"
```

```bash
# 匯入台股 FinLab 日線
curl -X POST "http://localhost:8080/api/v1/source/ingest/twstock/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": {
      "symbol": "6160",
      "name": "欣技",
      "source": "finlab",
      "asset_class": "STOCK",
      "file_name": "finlab_stocks_ohlcv.jsonl",
      "query_time": "2026-05-14T08:00:00Z"
    },
    "data": [
      {
        "date": "2026-05-14",
        "open": 20.45,
        "high": 21.50,
        "low": 20.45,
        "close": 21.10,
        "total_volume": 528,
        "total_ticks": 221
      }
    ]
  }'
```

```bash
# 直接匯入加密貨幣資料
curl -X POST "http://localhost:8080/api/v1/source/ingest/crypto/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": { "source": "bloomberg", "query_time": "2026-01-16T14:49:14Z" },
    "data": [
      {
        "ticker": "XBTUSD BGN Curncy",
        "date": "2026-01-16",
        "open": 95550.07,
        "high": 95825.34,
        "low": 95119.76,
        "close": 95709.01,
        "volume": 18500
      }
    ]
  }'
```

```bash
# 直接匯入港中混合資料（股票 + 指數）
curl -X POST "http://localhost:8080/api/v1/source/ingest/hkchina/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": { "source": "bloomberg", "query_time": "2026-04-08T02:29:42Z" },
    "data": [
      {
        "ticker": "700 HK Equity",
        "date": "2026-04-08",
        "open": 558.0,
        "high": 567.0,
        "low": 550.0,
        "close": 560.0,
        "volume": 23494910
      },
      {
        "ticker": "SH000911 Index",
        "date": "2026-04-08",
        "open": 5941.521,
        "high": 5966.992,
        "low": 5940.137,
        "close": 5960.012,
        "volume": 0
      }
    ]
  }'
```

```bash
# 直接匯入港中指數資料（相容舊流程）
curl -X POST "http://localhost:8080/api/v1/source/ingest/hkchina-index/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": { "source": "bloomberg", "query_time": "2026-04-08T02:29:42Z" },
    "data": [
      {
        "ticker": "HSI Index",
        "date": "2026-04-09",
        "open": 25772.56,
        "high": 25872.30,
        "low": 25711.23,
        "close": 25741.55,
        "volume": 0
      },
      {
        "ticker": "SH000911 Index",
        "date": "2026-04-08",
        "open": 5941.521,
        "high": 5966.992,
        "low": 5940.137,
        "close": 5960.012,
        "volume": 0
      }
    ]
  }'
```

```bash
# 直接匯入外匯資料
curl -X POST "http://localhost:8080/api/v1/source/ingest/fx/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": { "source": "bloomberg", "query_time": "2026-03-12T08:00:00Z" },
    "data": [
      {
        "pair": "EURUSD",
        "ticker": "EURUSD Curncy",
        "date": "2026-03-12",
        "open": 1.0498,
        "high": 1.0567,
        "low": 1.0489,
        "close": 1.0523
      }
    ]
  }'
```

```bash
# 直接匯入 WTX 期貨資料
curl -X POST "http://localhost:8080/api/v1/source/ingest/wtx/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": { "source": "bloomberg", "query_time": "2026-03-12T08:00:00Z" },
    "data": [
      {
        "symbol": "TXF1",
        "ticker": "TXF1 Index",
        "date": "2026-03-12",
        "open": 20800,
        "high": 21100,
        "low": 20700,
        "close": 21000,
        "volume": 50000
      }
    ]
  }'
```

---

### 批次管理端點

#### 查詢批次狀態

```
GET /api/v1/source/runs/{run_id}
```

| 參數     | 位置 | 類型 | 必填 | 說明        |
| -------- | ---- | ---- | ---- | ----------- |
| `run_id` | path | UUID | 是   | 攝取批次 ID |

回應範例：

```json
{
  "run_id": "019462f0-7c00-7000-8000-000000000001",
  "dataset_key": "crypto_eod",
  "status": "completed",
  "started_at": "2026-01-16T14:49:15Z",
  "completed_at": "2026-01-16T14:49:16Z",
  "total_records": 5,
  "success_records": 5,
  "failed_records": 0,
  "error_message": null
}
```

| 狀態值                  | 說明                                   |
| ----------------------- | -------------------------------------- |
| `pending`               | 已排入佇列，等待處理                   |
| `running`               | 正規化處理中                           |
| `completed`             | 處理完成                               |
| `completed_with_errors` | 已完成，但部分資料寫入或 DQ 流程有錯誤 |
| `failed`                | 處理失敗（查看 `error_message`）       |

#### curl 範例

```bash
curl "http://localhost:8080/api/v1/source/runs/019462f0-7c00-7000-8000-000000000001" \
  -H "X-API-Key: dev-source-key"
```

#### 重新執行正規化

以已儲存的原始 payload 重新觸發正規化流程，適用於修正正規化邏輯後重跑歷史資料。

```
POST /api/v1/source/runs/{run_id}/rerun
```

| 參數     | 位置 | 類型 | 必填 | 說明            |
| -------- | ---- | ---- | ---- | --------------- |
| `run_id` | path | UUID | 是   | 原始攝取批次 ID |

回應格式同 `IngestResponse`，會產生一筆**新的** `run_id`。

```bash
curl -X POST "http://localhost:8080/api/v1/source/runs/019462f0-7c00-7000-8000-000000000001/rerun" \
  -H "X-API-Key: dev-source-key"
```

#### 查詢可用資料集

```
GET /api/v1/source/datasets
```

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "dataset_key": "crypto_eod",
      "name": "Crypto EOD",
      "description": "Cryptocurrency end-of-day prices",
      "asset_class": "crypto",
      "market": "CRYPTO",
      "frequency": "daily",
      "is_active": true,
      "schema_id": null,
      "accepted_schema_versions": [],
      "current_schema_version": null,
      "schema_enforcement": null
    },
    {
      "dataset_key": "us_stock_eod",
      "name": "US Stock EOD",
      "description": "US equity end-of-day prices",
      "asset_class": "equity",
      "market": "US",
      "frequency": "daily",
      "is_active": true,
      "schema_id": null,
      "accepted_schema_versions": [],
      "current_schema_version": null,
      "schema_enforcement": null
    }
  ]
}
```

```bash
curl "http://localhost:8080/api/v1/source/datasets" \
  -H "X-API-Key: dev-source-key"
```

---

## Serve API（資料查詢）

**前綴**: `/api/v1/serve`
**認證**: 視 `SERVE_REQUIRE_AUTH` 設定

> Serve API 為**唯讀**，僅提供 Canonical 資料表的查詢功能。

### 標的查詢

#### 查詢標的清單

```
GET /api/v1/serve/instruments
```

| 參數          | 位置  | 類型   | 必填 | 說明                                                            |
| ------------- | ----- | ------ | ---- | --------------------------------------------------------------- |
| `market`      | query | string | 否   | 市場篩選（`CRYPTO`、`US`、`FX`、`TW`、`HK`、`CN`、`GLOBAL` 等） |
| `asset_class` | query | string | 否   | 資產類別篩選（`crypto`、`equity`、`index`、`fx`）               |
| `status`      | query | string | 否   | 狀態篩選（`active`、`delisted`）                                |
| `symbol`      | query | string | 否   | 精確代碼篩選                                                    |
| `page`        | query | int    | 否   | 頁碼（預設 1）                                                  |
| `page_size`   | query | int    | 否   | 每頁筆數（預設 100，最大 1000）                                 |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "instrument_id": "019462f0-7c00-7000-8000-000000000001",
      "asset_class": "crypto",
      "market": "CRYPTO",
      "symbol": "BTC",
      "name": "Bitcoin",
      "currency": "USD",
      "timezone": "UTC",
      "status": "active",
      "listed_date": null,
      "delisted_date": null
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 100,
    "total_records": 15,
    "total_pages": 1
  }
}
```

#### curl 範例

```bash
# 查詢所有加密貨幣標的
curl "http://localhost:8080/api/v1/serve/instruments?market=CRYPTO"

# 查詢美股 equity 類別
curl "http://localhost:8080/api/v1/serve/instruments?market=US&asset_class=equity"

# 精確查詢 BTC
curl "http://localhost:8080/api/v1/serve/instruments?symbol=BTC"
```

#### 查詢單一標的

```
GET /api/v1/serve/instruments/{instrument_id}
```

| 參數            | 位置 | 類型 | 必填 | 說明    |
| --------------- | ---- | ---- | ---- | ------- |
| `instrument_id` | path | UUID | 是   | 標的 ID |

```bash
curl "http://localhost:8080/api/v1/serve/instruments/019462f0-7c00-7000-8000-000000000001"
```

---

### 日K 資料

#### 查詢日K 清單

```
GET /api/v1/serve/eod
```

| 參數         | 位置  | 類型   | 必填 | 說明                               |
| ------------ | ----- | ------ | ---- | ---------------------------------- |
| `market`     | query | string | 否   | 市場篩選                           |
| `symbols`    | query | string | 否   | 代碼篩選（逗號分隔，如 `BTC,ETH`） |
| `start_date` | query | date   | 否   | 起始日期（`YYYY-MM-DD`）           |
| `end_date`   | query | date   | 否   | 結束日期（`YYYY-MM-DD`）           |
| `page`       | query | int    | 否   | 頁碼                               |
| `page_size`  | query | int    | 否   | 每頁筆數                           |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "instrument_id": "019462f0-7c00-7000-8000-000000000001",
      "symbol": "BTC",
      "name": "Bitcoin",
      "market": "CRYPTO",
      "trade_date": "2026-01-16",
      "open": 95550.07,
      "high": 95825.34,
      "low": 95119.76,
      "close": 95709.01,
      "volume": null,
      "turnover": null,
      "source": "bloomberg"
    }
  ],
  "pagination": { "page": 1, "page_size": 100, "total_records": 1, "total_pages": 1 }
}
```

#### curl 範例

```bash
# 查詢 BTC、ETH 近一個月日K
curl "http://localhost:8080/api/v1/serve/eod?market=CRYPTO&symbols=BTC,ETH&start_date=2026-01-01&end_date=2026-01-31"

# 查詢美股 AAPL 日K
curl "http://localhost:8080/api/v1/serve/eod?market=US&symbols=AAPL&start_date=2026-02-01"
```

#### 查詢單一標的日K

```
GET /api/v1/serve/eod/{instrument_id}
```

| 參數            | 位置  | 類型 | 必填 | 說明     |
| --------------- | ----- | ---- | ---- | -------- |
| `instrument_id` | path  | UUID | 是   | 標的 ID  |
| `start_date`    | query | date | 否   | 起始日期 |
| `end_date`      | query | date | 否   | 結束日期 |
| `page`          | query | int  | 否   | 頁碼     |
| `page_size`     | query | int  | 否   | 每頁筆數 |

```bash
curl "http://localhost:8080/api/v1/serve/eod/019462f0-7c00-7000-8000-000000000001?start_date=2026-01-01"
```

---

### 公司行為

#### 查詢公司行為清單

```
GET /api/v1/serve/corporate-actions
```

| 參數          | 位置  | 類型   | 必填 | 說明                                   |
| ------------- | ----- | ------ | ---- | -------------------------------------- |
| `market`      | query | string | 否   | 市場篩選                               |
| `symbols`     | query | string | 否   | 代碼篩選（逗號分隔）                   |
| `action_type` | query | string | 否   | 行為類型篩選（如 `dividend`、`split`） |
| `start_date`  | query | date   | 否   | 起始除權息日                           |
| `end_date`    | query | date   | 否   | 結束除權息日                           |
| `page`        | query | int    | 否   | 頁碼                                   |
| `page_size`   | query | int    | 否   | 每頁筆數                               |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "action_id": "...",
      "instrument_id": "...",
      "symbol": "AAPL",
      "name": "Apple Inc",
      "market": "US",
      "action_type": "dividend",
      "ex_date": "2026-02-07",
      "record_date": "2026-02-10",
      "pay_date": "2026-02-14",
      "ratio": null,
      "cash_amount": 0.25,
      "currency": "USD",
      "source": "bloomberg",
      "extra": null
    }
  ],
  "pagination": { "page": 1, "page_size": 100, "total_records": 1, "total_pages": 1 }
}
```

```bash
# 查詢美股除權息
curl "http://localhost:8080/api/v1/serve/corporate-actions?market=US&action_type=dividend"
```

#### 查詢單一標的公司行為

```
GET /api/v1/serve/corporate-actions/{instrument_id}
```

| 參數            | 位置  | 類型   | 必填 | 說明         |
| --------------- | ----- | ------ | ---- | ------------ |
| `instrument_id` | path  | UUID   | 是   | 標的 ID      |
| `action_type`   | query | string | 否   | 行為類型篩選 |
| `start_date`    | query | date   | 否   | 起始除權息日 |
| `end_date`      | query | date   | 否   | 結束除權息日 |
| `page`          | query | int    | 否   | 頁碼         |
| `page_size`     | query | int    | 否   | 每頁筆數     |

---

### 宏觀經濟指標

#### 查詢指標序列

```
GET /api/v1/serve/macro/series
```

| 參數          | 位置  | 類型   | 必填 | 說明             |
| ------------- | ----- | ------ | ---- | ---------------- |
| `market`      | query | string | 否   | 市場篩選         |
| `source`      | query | string | 否   | 資料來源篩選     |
| `source_code` | query | string | 否   | 來源代碼精確篩選 |
| `name`        | query | string | 否   | 序列名稱模糊搜尋 |
| `page`        | query | int    | 否   | 頁碼             |
| `page_size`   | query | int    | 否   | 每頁筆數         |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "series_id": "...",
      "name": "US CPI YoY",
      "unit": "percent",
      "frequency": "monthly",
      "market": "MACRO",
      "source_code": "CPI_YOY",
      "source": "bloomberg"
    }
  ],
  "pagination": { "page": 1, "page_size": 100, "total_records": 1, "total_pages": 1 }
}
```

```bash
# 列出所有宏觀序列
curl "http://localhost:8080/api/v1/serve/macro/series"

# 搜尋包含 "CPI" 的序列
curl "http://localhost:8080/api/v1/serve/macro/series?name=CPI"
```

#### 查詢觀測值清單

```
GET /api/v1/serve/macro/observations
```

| 參數          | 位置  | 類型   | 必填 | 說明         |
| ------------- | ----- | ------ | ---- | ------------ |
| `market`      | query | string | 否   | 市場篩選     |
| `source_code` | query | string | 否   | 來源代碼篩選 |
| `start_date`  | query | date   | 否   | 起始日期     |
| `end_date`    | query | date   | 否   | 結束日期     |
| `page`        | query | int    | 否   | 頁碼         |
| `page_size`   | query | int    | 否   | 每頁筆數     |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "id": "...",
      "series_id": "...",
      "series_name": "US CPI YoY",
      "obs_date": "2026-01-15",
      "value": 3.2,
      "source": "bloomberg"
    }
  ],
  "pagination": { "page": 1, "page_size": 100, "total_records": 1, "total_pages": 1 }
}
```

```bash
curl "http://localhost:8080/api/v1/serve/macro/observations?source_code=CPI_YOY&start_date=2025-01-01"
```

#### 查詢特定序列觀測值

```
GET /api/v1/serve/macro/observations/{series_id}
```

| 參數         | 位置  | 類型 | 必填 | 說明     |
| ------------ | ----- | ---- | ---- | -------- |
| `series_id`  | path  | UUID | 是   | 序列 ID  |
| `start_date` | query | date | 否   | 起始日期 |
| `end_date`   | query | date | 否   | 結束日期 |
| `page`       | query | int  | 否   | 頁碼     |
| `page_size`  | query | int  | 否   | 每頁筆數 |

---

### 期貨

#### 查詢期貨合約

```
GET /api/v1/serve/futures/contracts
```

| 參數            | 位置  | 類型   | 必填 | 說明                 |
| --------------- | ----- | ------ | ---- | -------------------- |
| `market`        | query | string | 否   | 市場篩選             |
| `symbols`       | query | string | 否   | 代碼篩選（逗號分隔） |
| `contract_code` | query | string | 否   | 合約代碼精確篩選     |
| `start_expiry`  | query | date   | 否   | 起始到期日           |
| `end_expiry`    | query | date   | 否   | 結束到期日           |
| `page`          | query | int    | 否   | 頁碼                 |
| `page_size`     | query | int    | 否   | 每頁筆數             |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "contract_id": "...",
      "instrument_id": "...",
      "symbol": "WTX",
      "name": "台指期",
      "contract_code": "TXFH6",
      "contract_month": "2026-03",
      "expiry_date": "2026-03-18",
      "currency": "TWD",
      "source": "bloomberg",
      "extra": null
    }
  ],
  "pagination": { "page": 1, "page_size": 100, "total_records": 1, "total_pages": 1 }
}
```

```bash
curl "http://localhost:8080/api/v1/serve/futures/contracts?market=WTX"
```

#### 查詢連續期貨日K

```
GET /api/v1/serve/futures/continuous
```

| 參數         | 位置  | 類型   | 必填 | 說明                 |
| ------------ | ----- | ------ | ---- | -------------------- |
| `market`     | query | string | 否   | 市場篩選             |
| `symbols`    | query | string | 否   | 代碼篩選（逗號分隔） |
| `start_date` | query | date   | 否   | 起始日期             |
| `end_date`   | query | date   | 否   | 結束日期             |
| `page`       | query | int    | 否   | 頁碼                 |
| `page_size`  | query | int    | 否   | 每頁筆數             |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "id": "...",
      "instrument_id": "...",
      "symbol": "WTX",
      "name": "台指期",
      "trade_date": "2026-01-16",
      "open": 22500.0,
      "high": 22650.0,
      "low": 22400.0,
      "close": 22600.0,
      "volume": 120000,
      "turnover": null,
      "source": "bloomberg",
      "roll_rule_id": "...",
      "roll_rule_name": "volume_based"
    }
  ],
  "pagination": { "page": 1, "page_size": 100, "total_records": 1, "total_pages": 1 }
}
```

```bash
curl "http://localhost:8080/api/v1/serve/futures/continuous?symbols=WTX&start_date=2026-01-01"
```

#### 查詢特定標的連續期貨日K

```
GET /api/v1/serve/futures/continuous/{instrument_id}
```

| 參數            | 位置  | 類型 | 必填 | 說明     |
| --------------- | ----- | ---- | ---- | -------- |
| `instrument_id` | path  | UUID | 是   | 標的 ID  |
| `start_date`    | query | date | 否   | 起始日期 |
| `end_date`      | query | date | 否   | 結束日期 |
| `page`          | query | int  | 否   | 頁碼     |
| `page_size`     | query | int  | 否   | 每頁筆數 |

---

### 債券

#### 查詢債券清單

```
GET /api/v1/serve/bonds
```

| 參數            | 位置  | 類型   | 必填 | 說明                   |
| --------------- | ----- | ------ | ---- | ---------------------- |
| `market`        | query | string | 否   | 市場代碼               |
| `symbols`       | query | string | 否   | 逗號分隔的債券代號     |
| `issuer`        | query | string | 否   | 發行人模糊查詢         |
| `maturity_from` | query | date   | 否   | 到期日起始日           |
| `maturity_to`   | query | date   | 否   | 到期日結束日           |
| `page`          | query | int    | 否   | 頁碼                   |
| `page_size`     | query | int    | 否   | 每頁筆數               |

#### 查詢債券日資料

```
GET /api/v1/serve/bonds/eod
```

| 參數         | 位置  | 類型   | 必填 | 說明                       |
| ------------ | ----- | ------ | ---- | -------------------------- |
| `market`     | query | string | 否   | 市場代碼                   |
| `symbols`    | query | string | 否   | 逗號分隔的債券代號         |
| `start_date` | query | date   | 否   | 起始日期                   |
| `end_date`   | query | date   | 否   | 結束日期                   |
| `page`       | query | int    | 否   | 頁碼                       |
| `page_size`  | query | int    | 否   | 每頁筆數                   |

債券日資料使用 `yield_to_maturity`、`clean_price`、`dirty_price`、`duration`，不與 OHLCV 型 EOD 混表。

---

### 交易日曆

```
GET /api/v1/serve/calendar
```

| 參數         | 位置  | 類型   | 必填   | 說明                                      |
| ------------ | ----- | ------ | ------ | ----------------------------------------- |
| `market`     | query | string | **是** | 市場代碼（必填）                          |
| `start_date` | query | date   | 否     | 起始日期                                  |
| `end_date`   | query | date   | 否     | 結束日期                                  |
| `is_open`    | query | bool   | 否     | 僅顯示開市日（`true`）或休市日（`false`） |
| `page`       | query | int    | 否     | 頁碼                                      |
| `page_size`  | query | int    | 否     | 每頁筆數                                  |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "market": "US",
      "trade_date": "2026-01-16",
      "is_open": true,
      "session_open": "09:30:00",
      "session_close": "16:00:00",
      "holiday_name": null
    },
    {
      "market": "US",
      "trade_date": "2026-01-19",
      "is_open": false,
      "session_open": null,
      "session_close": null,
      "holiday_name": "Martin Luther King Jr. Day"
    }
  ],
  "pagination": { "page": 1, "page_size": 100, "total_records": 2, "total_pages": 1 }
}
```

```bash
# 查詢美股 2026 年 1 月交易日
curl "http://localhost:8080/api/v1/serve/calendar?market=US&start_date=2026-01-01&end_date=2026-01-31"

# 只查詢休市日
curl "http://localhost:8080/api/v1/serve/calendar?market=US&is_open=false&start_date=2026-01-01&end_date=2026-12-31"
```

---

## Admin API（資料修正）

**前綴**: `/api/v1/admin`
**認證**: 必要（`X-API-Key`，無任何 bypass）

Admin API 用於人工修正 Canonical 資料。每次修正都會自動寫入 `canonical_correction` 表作為不可變的 audit log，記錄修改前後快照與操作者資訊。

| 方法  | 路徑                                      | 說明                                    |
| ----- | ----------------------------------------- | --------------------------------------- |
| GET   | `/dq-issues`                              | 查詢 DQ issue 清單                      |
| PATCH | `/eod/{instrument_id}/{trade_date}`       | 修正日K 的 EOD 欄位                     |
| PATCH | `/dq-issues/{issue_id}/resolve`           | 標記 DQ issue 為已解決                  |
| GET   | `/raw-payloads`                           | 查詢 raw payload 清單                   |
| GET   | `/raw-payloads/{run_id}`                  | 依 run_id 查詢原始 payload              |
| GET   | `/corrections`                            | 查詢修正 audit log                      |
| POST  | `/runs/bulk-rerun`                        | 批次重跑既有 runs                       |
| GET   | `/instrument-cache`                       | 讀取 `app/static/data/instruments.json` |
| PUT   | `/instrument-cache`                       | 全量覆蓋 instrument cache               |
| PATCH | `/instrument-cache/items/{instrument_id}` | 更新單一 instrument cache 項目          |

---

### 管理 Instrument Cache

#### 讀取快取

```
GET /api/v1/admin/instrument-cache
```

```bash
curl "http://localhost:8080/api/v1/admin/instrument-cache" \
  -H "X-API-Key: your-admin-key"
```

#### 全量覆蓋快取

```
PUT /api/v1/admin/instrument-cache
```

請求體需提供完整快取文件（`generated_at`、`total`、`markets`、`asset_classes`、`data`）。服務端會驗證欄位並重新排序/正規化後落檔。

```bash
curl -X PUT "http://localhost:8080/api/v1/admin/instrument-cache" \
  -H "X-API-Key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d @cache_payload.json
```

#### 更新單一標的

```
PATCH /api/v1/admin/instrument-cache/items/{instrument_id}
```

可更新欄位：`market`、`asset_class`、`symbol`、`name`、`currency`、`status`、`latest_trade_date`、`latest_price`。

```bash
curl -X PATCH "http://localhost:8080/api/v1/admin/instrument-cache/items/instrument-us-aapl" \
  -H "X-API-Key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Apple Inc.",
    "latest_trade_date": "2026-04-21",
    "latest_price": "191.23"
  }'
```

常見錯誤：

- `404`: 快取檔尚未產生，或指定 `instrument_id` 不存在
- `400`: 請求欄位格式不符或更新內容不合法

---

### 查詢 DQ Issues

```
GET /api/v1/admin/dq-issues
```

| 參數            | 位置  | 類型   | 必填 | 說明                            |
| --------------- | ----- | ------ | ---- | ------------------------------- |
| `resolved`      | query | bool   | 否   | 篩選已解決 / 未解決             |
| `instrument_id` | query | UUID   | 否   | 篩選特定標的                    |
| `severity`      | query | string | 否   | 篩選 `warning` / `error`        |
| `page`          | query | int    | 否   | 頁碼（預設 1）                  |
| `page_size`     | query | int    | 否   | 每頁筆數（預設 100，最大 1000） |

回應範例：

```json
{
  "success": true,
  "data": [
    {
      "id": "019462f0-7c00-7000-8000-000000000050",
      "instrument_id": "019462f0-7c00-7000-8000-000000000002",
      "issue_type": "MISSING_OHLC",
      "severity": "warning",
      "description": "Missing OHLC fields: high",
      "resolved": false,
      "resolved_at": null,
      "created_at": "2026-03-03T10:00:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 100,
    "total_records": 1,
    "total_pages": 1
  }
}
```

```bash
# 查全部未解決 issue
curl "http://localhost:8080/api/v1/admin/dq-issues?resolved=false" \
  -H "X-API-Key: your-admin-key"

# 查指定 instrument 的 warning
curl "http://localhost:8080/api/v1/admin/dq-issues?instrument_id=019462f0-7c00-7000-8000-000000000002&severity=warning" \
  -H "X-API-Key: your-admin-key"
```

---

### 修正日K 資料

```
PATCH /api/v1/admin/eod/{instrument_id}/{trade_date}
```

| 參數            | 位置 | 類型 | 必填 | 說明                   |
| --------------- | ---- | ---- | ---- | ---------------------- |
| `instrument_id` | path | UUID | 是   | 標的 ID                |
| `trade_date`    | path | date | 是   | 交易日（`YYYY-MM-DD`） |

#### 請求體格式（PatchEODRequest）

```json
{
  "correction_reason": "Bloomberg 原始資料錯誤，收盤價多一個零",
  "close": 153.0
}
```

| 欄位                | 類型    | 必填   | 說明                             |
| ------------------- | ------- | ------ | -------------------------------- |
| `correction_reason` | string  | **是** | 修正原因（將被記錄到 audit log） |
| `open`              | Decimal | 否     | 開盤價（null 表示清除）          |
| `high`              | Decimal | 否     | 最高價（null 表示清除）          |
| `low`               | Decimal | 否     | 最低價（null 表示清除）          |
| `close`             | Decimal | 否     | 收盤價（null 表示清除）          |
| `volume`            | int     | 否     | 成交量（null 表示清除）          |
| `total_ticks`       | int     | 否     | 總成交筆數（null 表示清除）      |
| `turnover`          | Decimal | 否     | 成交額（null 表示清除）          |

> 至少須提供一個 EOD 欄位，且新值必須與現有值不同，否則回傳 `400`。

#### 回應範例（200 OK）

```json
{
  "success": true,
  "correction_id": "019462f0-7c00-7000-8000-000000000099",
  "record_id": "019462f0-7c00-7000-8000-000000000001",
  "instrument_id": "019462f0-7c00-7000-8000-000000000002",
  "trade_date": "2026-01-16",
  "message": "EOD record corrected successfully"
}
```

`market_data_eod` 的 `record_id` 是由 `instrument_id` 與 `trade_date` 產生的穩定 logical UUID；同一商品同一天的日K修正會指向同一個 `record_id`。

#### 錯誤情境

| 狀態碼 | 說明                                            |
| ------ | ----------------------------------------------- |
| `404`  | instrument_id + trade_date 找不到對應的日K 記錄 |
| `400`  | 未提供任何 EOD 欄位，或新值與現有值完全相同     |

#### curl 範例

```bash
# 修正 BTC 2026-01-16 收盤價
curl -X PATCH \
  "http://localhost:8080/api/v1/admin/eod/019462f0-7c00-7000-8000-000000000002/2026-01-16" \
  -H "X-API-Key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "correction_reason": "Bloomberg 原始資料錯誤，收盤價多一個零",
    "close": 95709.01
  }'

# 同時修正多個欄位
curl -X PATCH \
  "http://localhost:8080/api/v1/admin/eod/{instrument_id}/2026-01-16" \
  -H "X-API-Key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "correction_reason": "OHLC 資料整體異常，人工校正",
    "open": 95550.07,
    "high": 95825.34,
    "low": 95119.76,
    "close": 95709.01
  }'

# 清除欄位（設為 null）
curl -X PATCH \
  "http://localhost:8080/api/v1/admin/eod/{instrument_id}/2026-01-16" \
  -H "X-API-Key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "correction_reason": "成交額資料來源錯誤，暫時清除",
    "turnover": null
  }'
```

---

### 標記 DQ Issue 已解決

```
PATCH /api/v1/admin/dq-issues/{issue_id}/resolve
```

| 參數       | 位置 | 類型 | 必填 | 說明        |
| ---------- | ---- | ---- | ---- | ----------- |
| `issue_id` | path | UUID | 是   | DQ issue ID |

#### 請求體格式（ResolveDQIssueRequest）

```json
{
  "correction_reason": "已確認為資料來源暫時性錯誤，資料已重新攝取修正"
}
```

| 欄位                | 類型   | 必填   | 說明                             |
| ------------------- | ------ | ------ | -------------------------------- |
| `correction_reason` | string | **是** | 解決原因（將被記錄到 audit log） |

#### 回應範例（200 OK）

```json
{
  "success": true,
  "correction_id": "019462f0-7c00-7000-8000-000000000099",
  "issue_id": "019462f0-7c00-7000-8000-000000000050",
  "resolved_at": "2026-03-03T10:00:00Z",
  "message": "DQ issue resolved successfully"
}
```

#### 錯誤情境

| 狀態碼 | 說明                             |
| ------ | -------------------------------- |
| `404`  | issue_id 找不到對應的 DQ issue   |
| `409`  | 該 DQ issue 已經是 resolved 狀態 |

#### curl 範例

```bash
# 標記 DQ issue 為已解決
curl -X PATCH \
  "http://localhost:8080/api/v1/admin/dq-issues/019462f0-7c00-7000-8000-000000000050/resolve" \
  -H "X-API-Key: your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "correction_reason": "已確認為資料來源暫時性錯誤，資料已重新攝取修正"
  }'
```

---

### 查詢 Raw Payload

#### 查詢清單

```
GET /api/v1/admin/raw-payloads
```

| 參數          | 位置  | 類型   | 必填 | 說明                          |
| ------------- | ----- | ------ | ---- | ----------------------------- |
| `dataset_key` | query | string | 否   | 篩選特定 dataset              |
| `run_id`      | query | UUID   | 否   | 篩選特定 run                  |
| `date_from`   | query | date   | 否   | `created_at >=` 這一天（UTC） |
| `date_to`     | query | date   | 否   | `created_at <` 次日（UTC）    |
| `page`        | query | int    | 否   | 頁碼（預設 1）                |
| `page_size`   | query | int    | 否   | 每頁筆數（預設 20，最大 200） |

```bash
# 查最新 raw payload
curl "http://localhost:8080/api/v1/admin/raw-payloads" \
  -H "X-API-Key: your-admin-key"

# 依 dataset_key 篩選
curl "http://localhost:8080/api/v1/admin/raw-payloads?dataset_key=crypto_bloomberg_eod&page_size=10" \
  -H "X-API-Key: your-admin-key"

# 依日期區間篩選
curl "http://localhost:8080/api/v1/admin/raw-payloads?date_from=2026-04-01&date_to=2026-04-08" \
  -H "X-API-Key: your-admin-key"
```

#### 依 run_id 查詢單筆 raw payload

```
GET /api/v1/admin/raw-payloads/{run_id}
```

```bash
curl "http://localhost:8080/api/v1/admin/raw-payloads/019462f0-7c00-7000-8000-000000000001" \
  -H "X-API-Key: your-admin-key"
```

此端點適合用來：

- 追蹤特定 ingestion run 的原始輸入
- 重新比對上游 payload 與 canonical 寫入結果
- 配合 Source API rerun / Admin bulk rerun 做除錯

---

### 查詢修正紀錄

```
GET /api/v1/admin/corrections
```

| 參數            | 位置  | 類型   | 必填 | 說明                                              |
| --------------- | ----- | ------ | ---- | ------------------------------------------------- |
| `table_name`    | query | string | 否   | 篩選修正的資料表（`market_data_eod`、`dq_issue`） |
| `instrument_id` | query | UUID   | 否   | 篩選特定標的的修正紀錄                            |
| `page`          | query | int    | 否   | 頁碼（預設 1）                                    |
| `page_size`     | query | int    | 否   | 每頁筆數（預設 100，最大 1000）                   |

回應為**最新優先**排序。

`market_data_eod` correction 的 `record_id` 為穩定 logical UUID；`dq_issue` correction 的 `record_id` 為對應 issue UUID。

#### 回應範例（200 OK）

```json
{
  "success": true,
  "data": [
    {
      "id": "019462f0-7c00-7000-8000-000000000099",
      "table_name": "market_data_eod",
      "record_id": "019462f0-7c00-7000-8000-000000000001",
      "instrument_id": "019462f0-7c00-7000-8000-000000000002",
      "trade_date": "2026-01-16",
      "corrected_by": "your****",
      "correction_reason": "Bloomberg 原始資料錯誤，收盤價多一個零",
      "before_snapshot": { "close": "1530000" },
      "after_snapshot": { "close": "153000" },
      "created_at": "2026-03-03T10:00:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_records": 1,
    "total_pages": 1
  }
}
```

> **`corrected_by` 遮罩**：回應中的 API Key 僅顯示前 4 碼加 `****`（如 `your****`），不會洩漏完整金鑰。

#### curl 範例

```bash
# 查詢所有修正紀錄
curl "http://localhost:8080/api/v1/admin/corrections" \
  -H "X-API-Key: your-admin-key"

# 篩選 EOD 資料的修正紀錄
curl "http://localhost:8080/api/v1/admin/corrections?table_name=market_data_eod" \
  -H "X-API-Key: your-admin-key"

# 篩選特定標的的修正紀錄
curl "http://localhost:8080/api/v1/admin/corrections?instrument_id=019462f0-7c00-7000-8000-000000000002" \
  -H "X-API-Key: your-admin-key"

# 篩選 DQ issue 解決記錄
curl "http://localhost:8080/api/v1/admin/corrections?table_name=dq_issue&page_size=50" \
  -H "X-API-Key: your-admin-key"
```

---

### 批次重跑既有 Run

```
POST /api/v1/admin/runs/bulk-rerun
```

| 參數          | 位置  | 類型   | 必填 | 說明                                                |
| ------------- | ----- | ------ | ---- | --------------------------------------------------- |
| `dataset_key` | query | string | 否   | 只重跑特定 dataset                                  |
| `status`      | query | string | 否   | 篩選 `completed`、`failed`、`all`，預設 `completed` |
| `limit`       | query | int    | 否   | 單次排入上限，預設 100，範圍 1–1000                 |

回應範例：

```json
{
  "success": true,
  "queued": 3,
  "skipped": 1,
  "errors": 0,
  "new_run_ids": [
    "019462f0-7c00-7000-8000-000000000101",
    "019462f0-7c00-7000-8000-000000000102",
    "019462f0-7c00-7000-8000-000000000103"
  ],
  "error_details": []
}
```

```bash
# 重跑最早的 100 個 completed runs
curl -X POST "http://localhost:8080/api/v1/admin/runs/bulk-rerun" \
  -H "X-API-Key: your-admin-key"

# 只重跑某個 dataset 的 failed runs
curl -X POST "http://localhost:8080/api/v1/admin/runs/bulk-rerun?dataset_key=crypto_bloomberg_eod&status=failed" \
  -H "X-API-Key: your-admin-key"
```

> `bulk-rerun` 會為符合條件且未超過 `limit` 的 run 建立新的 `ingestion_run`，並重新排入 normalize，不會覆寫舊的 run 記錄。大量重跑應分批執行並指定 dataset。

---

## 分頁機制

所有列表端點支援分頁，參數統一為：

| 參數        | 預設值 | 範圍   | 說明     |
| ----------- | ------ | ------ | -------- |
| `page`      | 1      | ≥ 1    | 頁碼     |
| `page_size` | 100    | 1–1000 | 每頁筆數 |

回應中的 `pagination` 物件：

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

### 遍歷所有頁面

```bash
# 第 1 頁
curl "http://localhost:8080/api/v1/serve/instruments?page=1&page_size=50"

# 第 2 頁
curl "http://localhost:8080/api/v1/serve/instruments?page=2&page_size=50"
```

---

## 錯誤代碼一覽

### HTTP 狀態碼

| 狀態碼 | 說明       | 常見原因                                                      |
| ------ | ---------- | ------------------------------------------------------------- |
| `200`  | 成功       | 請求正常處理                                                  |
| `400`  | 請求錯誤   | dataset 不存在、market 不符、payload 驗證失敗、dataset 已停用 |
| `401`  | 未認證     | 未帶入 `X-API-Key` Header                                     |
| `403`  | 禁止存取   | API Key 無效、IP 不在允許名單                                 |
| `404`  | 找不到資源 | instrument_id / run_id / series_id / issue_id 不存在          |
| `409`  | 衝突       | 操作與現有狀態衝突（如重複標記已解決的 DQ issue）             |
| `422`  | 驗證錯誤   | 請求體格式不符 Pydantic schema                                |
| `429`  | 請求過多   | 超過限流上限                                                  |
| `500`  | 伺服器錯誤 | 內部錯誤、未設定 API Key、未設定允許名單（生產環境）          |

### 錯誤回應格式

```json
{
  "detail": "Invalid API key"
}
```

### Source API 常見錯誤

| 錯誤訊息                      | 狀態碼 | 說明                           |
| ----------------------------- | ------ | ------------------------------ |
| `"Missing API key"`           | 401    | 未帶入 X-API-Key               |
| `"Invalid API key"`           | 403    | API Key 不正確                 |
| `"Source API client IP not allowlisted"` | 403    | IP 不在 nginx Source API allowlist |
| `"Rate limit exceeded"`       | 429    | 請求頻率超過限制               |
| `"Dataset 'xxx' not found"`   | 400    | 指定的 dataset_key 不存在      |
| `"Dataset 'xxx' is inactive"` | 400    | 資料集已停用                   |
| `"Market mismatch..."`        | 400    | payload 市場與端點市場不符     |

### Admin API 常見錯誤

| 錯誤訊息                              | 狀態碼 | 說明                                         |
| ------------------------------------- | ------ | -------------------------------------------- |
| `"Missing API key"`                   | 401    | 未帶入 X-API-Key                             |
| `"Invalid API key"`                   | 403    | Admin API Key 不正確                         |
| `"No admin API key configured"`      | 500    | 未設定 `ADMIN_API_KEY` 環境變數             |
| `"EOD record not found..."`           | 404    | 指定的 instrument_id + trade_date 無日K 記錄 |
| `"DQ issue ... not found"`            | 404    | 指定的 issue_id 不存在                       |
| `"Raw payload not found"`             | 404    | 指定的 run_id 找不到 raw payload             |
| `"Instrument cache not found..."`     | 404    | 尚未產生 `app/static/data/instruments.json`  |
| `"Instrument ... not found in cache"` | 404    | instrument cache 中找不到指定 instrument_id  |
| `"Instrument cache ... invalid"`      | 400    | instrument cache 文件格式不合法              |
| `"No OHLCV fields provided..."`       | 400    | PATCH 請求未包含任何 OHLCV 欄位              |
| `"No changes detected..."`            | 400    | 提交的值與現有值完全相同                     |
| `"DQ issue ... is already resolved"`  | 409    | 該 DQ issue 已是 resolved 狀態               |

---

## Python 範例

### 安裝依賴

```bash
pip install httpx
```

### Source API：攝取資料

```python
import httpx

BASE_URL = "http://localhost:8080"
API_KEY = "dev-source-key"
HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
}


def ingest_crypto_data(payload: dict) -> dict:
    """攝取加密貨幣資料"""
    with httpx.Client() as client:
        resp = client.post(
            f"{BASE_URL}/api/v1/source/ingest/crypto",
            headers=HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


# 使用範例
payload = {
    "dataset_key": "crypto_eod",
    "source": "bloomberg",
    "request_key": "bloomberg_crypto_20260116_144914",
    "idempotency_key": "bloomberg_crypto_20260116_144914",
    "payload": {
        "metadata": {
            "source": "Bloomberg API",
            "category": "Cryptocurrency",
            "query_time": "2026-01-16T14:49:14",
            "total_records": 1,
        },
        "data": [
            {
                "crypto_id": "bitcoin",
                "symbol": "BTC",
                "name": "Bitcoin",
                "ticker": "XBTUSD BGN Curncy",
                "price": {"last": 95709.01, "open": 95550.07, "high": 95825.34, "low": 95119.76},
                "change": {"net": 158.81, "percent_1d": 0.1662},
                "timestamp": {"query_time": "2026-01-16T14:49:14", "last_update": "2026-01-16"},
                "metadata": {"source": "Bloomberg", "data_type": "cryptocurrency"},
            }
        ],
    },
    "fetched_at": "2026-01-16T14:49:14Z",
}

result = ingest_crypto_data(payload)
print(f"Run ID: {result['run_id']}")
print(f"Status: {result['status']}")
```

### Source API：Direct 格式攝取

```python
def ingest_usstock_direct(filepath: str) -> dict:
    """從 Bloomberg 匯出檔直接攝取美股資料"""
    import json

    with open(filepath, "r") as f:
        data = json.load(f)

    with httpx.Client() as client:
        resp = client.post(
            f"{BASE_URL}/api/v1/source/ingest/usstock/direct",
            headers=HEADERS,
            json=data,
        )
        resp.raise_for_status()
        return resp.json()
```

### Source API：查詢批次狀態

```python
def check_run_status(run_id: str) -> dict:
    """查詢攝取批次狀態"""
    with httpx.Client() as client:
        resp = client.get(
            f"{BASE_URL}/api/v1/source/runs/{run_id}",
            headers=HEADERS,
        )
        resp.raise_for_status()
        return resp.json()


# 等待處理完成
import time

result = ingest_crypto_data(payload)
run_id = result["run_id"]

while True:
    status = check_run_status(run_id)
    print(f"Status: {status['status']}")
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(1)

print(f"成功: {status['success_records']}, 失敗: {status['failed_records']}")
```

### Serve API：查詢標的

```python
def list_instruments(market: str = None, page: int = 1, page_size: int = 100) -> dict:
    """查詢標的清單"""
    params = {"page": page, "page_size": page_size}
    if market:
        params["market"] = market

    with httpx.Client() as client:
        resp = client.get(
            f"{BASE_URL}/api/v1/serve/instruments",
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


# 查詢所有加密貨幣標的
instruments = list_instruments(market="CRYPTO")
for inst in instruments["data"]:
    print(f"{inst['symbol']}: {inst['name']} ({inst['status']})")
```

### Serve API：查詢日K 資料

```python
def get_eod_data(
    market: str = None,
    symbols: str = None,
    start_date: str = None,
    end_date: str = None,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    """查詢日K 資料"""
    params = {"page": page, "page_size": page_size}
    if market:
        params["market"] = market
    if symbols:
        params["symbols"] = symbols
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    with httpx.Client() as client:
        resp = client.get(
            f"{BASE_URL}/api/v1/serve/eod",
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


# 查詢 BTC 一月份日K
eod = get_eod_data(market="CRYPTO", symbols="BTC", start_date="2026-01-01", end_date="2026-01-31")
for row in eod["data"]:
    print(f"{row['trade_date']}: O={row['open']} H={row['high']} L={row['low']} C={row['close']}")
```

### Serve API：遍歷所有頁面

```python
def fetch_all_pages(endpoint: str, params: dict = None) -> list:
    """自動遍歷所有分頁，回傳完整資料列表"""
    all_data = []
    page = 1
    params = params or {}

    with httpx.Client() as client:
        while True:
            params["page"] = page
            resp = client.get(f"{BASE_URL}{endpoint}", params=params)
            resp.raise_for_status()
            result = resp.json()

            all_data.extend(result["data"])

            pagination = result["pagination"]
            if page >= pagination["total_pages"]:
                break
            page += 1

    return all_data


# 取得所有美股標的
all_us_instruments = fetch_all_pages(
    "/api/v1/serve/instruments",
    params={"market": "US", "page_size": 1000},
)
print(f"共 {len(all_us_instruments)} 筆美股標的")
```

### Admin API：修正日K 資料

```python
import httpx

BASE_URL = "http://localhost:8080"
ADMIN_KEY = "your-admin-key"
ADMIN_HEADERS = {
    "X-API-Key": ADMIN_KEY,
    "Content-Type": "application/json",
}


def patch_eod(instrument_id: str, trade_date: str, fields: dict, reason: str) -> dict:
    """修正日K 資料的 OHLCV 欄位"""
    payload = {"correction_reason": reason, **fields}
    with httpx.Client() as client:
        resp = client.patch(
            f"{BASE_URL}/api/v1/admin/eod/{instrument_id}/{trade_date}",
            headers=ADMIN_HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


# 修正收盤價
result = patch_eod(
    instrument_id="019462f0-7c00-7000-8000-000000000002",
    trade_date="2026-01-16",
    fields={"close": 95709.01},
    reason="Bloomberg 原始資料收盤價多一個零，人工校正",
)
print(f"Correction ID: {result['correction_id']}")
```

### Admin API：標記 DQ Issue 已解決

```python
def resolve_dq_issue(issue_id: str, reason: str) -> dict:
    """標記 DQ issue 為已解決"""
    with httpx.Client() as client:
        resp = client.patch(
            f"{BASE_URL}/api/v1/admin/dq-issues/{issue_id}/resolve",
            headers=ADMIN_HEADERS,
            json={"correction_reason": reason},
        )
        resp.raise_for_status()
        return resp.json()


result = resolve_dq_issue(
    issue_id="019462f0-7c00-7000-8000-000000000050",
    reason="已確認為資料來源暫時性錯誤，資料已重新攝取修正",
)
print(f"Resolved at: {result['resolved_at']}")
```

### Admin API：查詢修正紀錄

```python
def list_corrections(
    table_name: str = None,
    instrument_id: str = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """查詢修正 audit log"""
    params = {"page": page, "page_size": page_size}
    if table_name:
        params["table_name"] = table_name
    if instrument_id:
        params["instrument_id"] = instrument_id

    with httpx.Client() as client:
        resp = client.get(
            f"{BASE_URL}/api/v1/admin/corrections",
            headers=ADMIN_HEADERS,
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


# 查詢某個標的的所有修正紀錄
corrections = list_corrections(instrument_id="019462f0-7c00-7000-8000-000000000002")
for c in corrections["data"]:
    print(f"{c['created_at']} [{c['table_name']}] {c['correction_reason']}")
    print(f"  Before: {c['before_snapshot']}")
    print(f"  After:  {c['after_snapshot']}")
```

### 完整工作流程範例

```python
"""
完整工作流程：攝取 → 等待處理 → 查詢結果
"""
import httpx
import time

BASE_URL = "http://localhost:8080"
SOURCE_KEY = "dev-source-key"

# 1. 攝取資料
print("1. 攝取加密貨幣資料...")
with httpx.Client() as client:
    resp = client.post(
        f"{BASE_URL}/api/v1/source/ingest/crypto",
        headers={"X-API-Key": SOURCE_KEY, "Content-Type": "application/json"},
        json={
            "dataset_key": "crypto_eod",
            "source": "bloomberg",
            "request_key": "demo_20260116",
            "idempotency_key": "demo_20260116",
            "payload": {
                "metadata": {"source": "Bloomberg API", "total_records": 1},
                "data": [{
                    "crypto_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
                    "ticker": "XBTUSD BGN Curncy",
                    "price": {"last": 95709.01, "open": 95550.07, "high": 95825.34, "low": 95119.76},
                    "change": {"net": 158.81, "percent_1d": 0.1662},
                    "timestamp": {"query_time": "2026-01-16T14:49:14", "last_update": "2026-01-16"},
                    "metadata": {"source": "Bloomberg", "data_type": "cryptocurrency"},
                }],
            },
            "fetched_at": "2026-01-16T14:49:14Z",
        },
    )
    ingest_result = resp.json()
    run_id = ingest_result["run_id"]
    print(f"   Run ID: {run_id}")

# 2. 等待處理完成
print("2. 等待處理完成...")
with httpx.Client() as client:
    for _ in range(30):
        resp = client.get(
            f"{BASE_URL}/api/v1/source/runs/{run_id}",
            headers={"X-API-Key": SOURCE_KEY},
        )
        status_info = resp.json()
        print(f"   狀態: {status_info['status']}")
        if status_info["status"] in ("completed", "failed"):
            break
        time.sleep(1)

# 3. 查詢結果
print("3. 查詢 Canonical 資料...")
with httpx.Client() as client:
    resp = client.get(
        f"{BASE_URL}/api/v1/serve/eod",
        params={"market": "CRYPTO", "symbols": "BTC"},
    )
    eod_data = resp.json()
    print(f"   共 {eod_data['pagination']['total_records']} 筆日K 資料")
    for row in eod_data["data"][:5]:
        print(f"   {row['trade_date']}: close={row['close']}")
```

---

## 常見問題

### Q: 收到 403 Forbidden，該怎麼辦？

**可能原因：**

1. **API Key 錯誤** — 確認 `X-API-Key` Header 的值與 `SOURCE_API_KEY` 環境變數一致
2. **IP 不在允許名單** — 生產環境由 nginx 回覆 403，請確認你的 IP 在 `SOURCE_ALLOWLIST_CIDRS` 內
3. **API Key 未注入 Docker 環境** — 確認 `docker-compose.yml` 的 `SOURCE_API_KEY: ${SOURCE_API_KEY:-dev-source-key}` 有讀到 `.env`，未設定時才會使用 `dev-source-key`

**解法：** 本機測試可使用 `dev-source-key`，或在 `.env` 設定 `SOURCE_API_KEY` 後重啟 app container。

### Q: 相同的 idempotency_key 送了兩次會怎樣？

系統會回傳第一次的 `run_id`，不會重新處理。回應訊息為 `"Duplicate idempotency_key, returning existing run"`。

### Q: Serve API 需不需要 API Key？

預設不需要（`SERVE_REQUIRE_AUTH=false`）。若要啟用認證，設定環境變數：

```env
SERVE_REQUIRE_AUTH=true
```

然後用 Admin API 建立 Serve key。`SERVE_API_KEYS` 仍可作為過渡 fallback；新 consumer 應使用 DB-backed key。

### Q: 如何查看所有可用的 dataset_key？

```bash
curl "http://localhost:8080/api/v1/source/datasets" \
  -H "X-API-Key: dev-source-key"
```

### Q: 日K 資料的 OHLCV 欄位可能是 null 嗎？

是的。`open`、`high`、`low`、`close`、`volume`、`turnover` 都是可選欄位。
部分資料來源不一定提供完整 OHLCV。

### Q: 原始資料保留多久？

目前預設不啟用自動刪除（`RAW_RETENTION_ENABLED=false`）。
若之後啟用 retention，則會依 `RAW_RETENTION_DAYS=14` 計算過期時間，超過期限的 raw payload 會被清理服務刪除。
過期後就無法再用 `/runs/{run_id}/rerun` 重跑。

### Q: 支援哪些市場？

可用 `/api/v1/source/datasets` 查詢。目前支援的市場代碼：

| 市場代碼 | 說明             |
| -------- | ---------------- |
| `CRYPTO` | 加密貨幣         |
| `US`     | 美國股市         |
| `FX`     | 全球外匯         |
| `MACRO`  | 宏觀經濟         |
| `WTX`    | 台灣加權指數期貨 |
| `GLOBAL` | 全球市場         |
| `TW`     | 台灣市場         |
| `HK`     | 香港市場         |
| `CN`     | 中國市場         |

### Q: Admin API 修正後，原始資料會被刪除嗎？

不會。Admin API 只修改 Canonical 層的資料（`market_data_eod` 等），原始 payload 在 `raw.market_payload` 中保持不變。每次修正都會新增一筆 `canonical_correction` 記錄，包含修改前後的快照，可完整追溯所有變更。

### Q: 修正後可以再次修正同一筆資料嗎？

可以。每次呼叫 PATCH 端點都會新增一筆獨立的 audit log，無論修正幾次都有完整歷史紀錄。

### Q: `corrected_by` 欄位顯示的是什麼？

回應中的 `corrected_by` 欄位會將 API Key 遮罩，只顯示前 4 個字元加 `****`（例如 `your****`）。完整金鑰永遠不會出現在 API 回應中。

### Q: DQ issue 標記為 resolved 之後可以撤銷嗎？

目前不支援撤銷 DQ issue 的 resolved 狀態。如需撤銷，請聯繫系統管理員直接修改資料庫，並手動新增一筆說明性的 audit log。

### Q: 生產環境需要注意什麼？

1. **必須**設定 `SOURCE_ALLOWLIST_CIDRS`，部署流程會用它產生 nginx `/api/v1/source/*` allowlist；本機 loopback（`127.0.0.1/32`、`::1/128`）會自動加入
   - 若服務在 Cloudflare 後方，nginx 必須先透過 `real_ip_header CF-Connecting-IP` 還原真實 client IP，否則 allowlist 會用到 Cloudflare edge IP 而誤擋；詳見 `cloudflare-nginx-source-allowlist-incident.md`
2. **必須**設定 `ADMIN_API_KEY`（否則所有 Admin API 端點回傳 500）
3. **建議**啟用 `SERVE_REQUIRE_AUTH=true`
4. **建議**設定 `CORS allow_origins` 為特定網域（目前預設 `*`）
5. **建議**使用反向代理（如 nginx）處理 HTTPS
6. **建議** Admin API Key 與 Source/Serve API Key 分開管理，限制知曉範圍

### Q: 如何查看互動式 API 文件？

瀏覽器開啟 [http://localhost:8080/docs](http://localhost:8080/docs)（Swagger UI）
或 [http://localhost:8080/redoc](http://localhost:8080/redoc)（ReDoc 格式）。

---

## 環境變數參考

| 變數                         | 預設值                                                  | 說明                                                       |
| ---------------------------- | ------------------------------------------------------- | ---------------------------------------------------------- |
| `DATABASE_URL`               | `postgresql+asyncpg://findb:findb@localhost:5435/findb` | PostgreSQL 連線字串                                        |
| `SOURCE_API_KEY`            | （空）                                                  | Source API 金鑰                                             |
| `SOURCE_ALLOWLIST_CIDRS`     | `127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | nginx `/api/v1/source/*` IP 允許名單（CIDR，逗號分隔）     |
| `SOURCE_TRUST_PROXY_HEADERS` | `false`                                                 | 是否信任 X-Forwarded-For（rate limit client IP 用）        |
| `SERVE_API_KEYS`             | （空）                                                  | Serve API env fallback（過渡用；新 key 使用 Admin API 建入 DB） |
| `SERVE_REQUIRE_AUTH`         | `false`                                                 | Serve API 是否需要認證                                     |
| `ADMIN_API_KEY`             | （空）                                                  | Admin API 金鑰，**必須設定**才能使用 Admin API              |
| `RATE_LIMIT_REQUESTS`        | `100`                                                   | 限流上限（每 window 內的請求數）                           |
| `RATE_LIMIT_WINDOW`          | `60`                                                    | 限流時間窗口（秒）                                         |
| `RAW_RETENTION_ENABLED`      | `false`                                                 | 是否啟用原始資料過期清理                                   |
| `RAW_RETENTION_DAYS`         | `14`                                                    | 原始資料保留天數                                           |
| `DEBUG`                      | `false`                                                 | 除錯模式                                                    |
