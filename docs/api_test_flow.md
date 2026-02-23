# API Test Flow

完整測試流程（Docker 環境）。涵蓋資料庫初始化、資料種子、Source API ingest、Serve API 查詢。

## 0. 前置準備

1. 複製環境設定檔

```bash
cp .env.example .env
```

2. 啟動 Docker 服務

```bash
docker-compose up -d --build
```

## 1. 資料庫初始化與種子

在容器內執行 seed：

```bash
docker-compose exec app python /app/scripts/seed_data.py
```

確認已寫入多個 dataset 與五個加密貨幣標的。

## 2. Source API 測試

### 2.1 Ingest 原始資料

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/crypto" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@scripts/sample_ingest_payload.json"
```

`sample_ingest_payload.json` 內含 `idempotency_key`，用於請求去重。

若使用 Bloomberg 直接格式（`metadata + data`）：

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/usstock/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@bloomberg_usstock_20260204_160051_api_format.json"
```

區域市場 index dataset 可使用市場專屬 ingest 路徑（例如 TW/HK/CN）：

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/tw" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@scripts/sample_ingest_payload.json"
```

預期回傳：

```json
{
  "success": true,
  "run_id": "...",
  "status": "pending",
  "message": "Data received, processing queued"
}
```

### 2.2 查詢 ingestion run 狀態

將 `run_id` 代入：

```bash
curl -X GET "http://localhost:8000/api/v1/source/runs/{run_id}" \
  -H "X-API-Key: dev-source-key"
```

### 2.3 查詢可用 dataset

```bash
curl -X GET "http://localhost:8000/api/v1/source/datasets" \
  -H "X-API-Key: dev-source-key"
```

## 3. Serve API 測試

### 3.1 查詢標的清單

```bash
curl -X GET "http://localhost:8000/api/v1/serve/instruments?market=CRYPTO&page=1&page_size=100"
```

### 3.2 查詢日K資料

```bash
curl -X GET "http://localhost:8000/api/v1/serve/eod?market=CRYPTO&symbols=BTC,ETH&start_date=2026-01-01&end_date=2026-01-16&page=1&page_size=100"
```

### 3.3 查詢單一標的日K

先取得 `instrument_id`，再代入：

```bash
curl -X GET "http://localhost:8000/api/v1/serve/eod/{instrument_id}?start_date=2026-01-01&end_date=2026-01-16&page=1&page_size=100"
```

### 3.4 查詢交易日曆

```bash
curl -X GET "http://localhost:8000/api/v1/serve/calendar?market=CRYPTO&start_date=2026-01-01&end_date=2026-01-16&page=1&page_size=100"
```

## 4. 故障排查

- `ForeignKeyViolationError`：未先執行 seed，請先跑 `seed_data.py`
- `Missing API key`：確認 header `X-API-Key: dev-source-key`
- `Connection refused`：確認 Docker 是否啟動、`docker-compose ps` 顯示 healthy
