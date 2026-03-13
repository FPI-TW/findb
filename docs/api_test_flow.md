# API 測試流程

> **最後更新**: 2026-03-13

完整手動測試流程（Docker 環境）。涵蓋服務啟動、資料種子、安全機制驗證、Source API 攝取、Serve API 全端點查詢、Admin API 資料修正，以及端到端煙霧測試。

自動化測試請見 [§8. 自動化測試](#8-自動化測試)。

---

## 目錄

- [0. 前置準備](#0-前置準備)
- [1. 服務驗證](#1-服務驗證)
- [2. 安全機制測試](#2-安全機制測試)
- [3. Source API 測試](#3-source-api-測試)
- [4. Serve API 測試](#4-serve-api-測試)
- [5. Admin API 測試](#5-admin-api-測試)
- [6. 端到端煙霧測試](#6-端到端煙霧測試)
- [7. 視覺化測試面板](#7-視覺化測試面板)
- [8. 自動化測試](#8-自動化測試)
- [9. 故障排查](#9-故障排查)

---

## 0. 前置準備

### 0.1 複製環境設定

```bash
cp .env.example .env
```

> `docker-compose.yml` 使用 `${SOURCE_API_KEYS:-dev-source-key}` 語法。
> 若 `.env` 未設定 `SOURCE_API_KEYS`，預設使用 `dev-source-key`。

### 0.2 啟動 Docker 服務

```bash
docker-compose up -d --build
```

確認所有服務就緒：

```bash
docker-compose ps
```

預期看到 `findb-app`、`findb-postgres`、`findb-raw-cleanup`、`findb-pgadmin` 皆為 running。

### 0.3 初始化資料種子

```bash
docker-compose exec app python /app/scripts/seed_data.py
```

會寫入 20+ 個 dataset 定義（含 Bloomberg Direct 格式）與加密貨幣/美股/外匯等標的。

---

## 1. 服務驗證

### 1.1 健康檢查

```bash
curl http://localhost:8000/health
```

預期回傳：

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "source_allowlist_configured": false
}
```

> `source_allowlist_configured` 為 `false` 表示未設定 IP 允許名單（開發模式下正常）。

### 1.2 根端點

```bash
curl http://localhost:8000/
```

預期回傳：

```json
{
  "name": "FinDB",
  "version": "0.1.0",
  "docs": "/docs"
}
```

### 1.3 Swagger UI

瀏覽器開啟 [http://localhost:8000/docs](http://localhost:8000/docs)，確認可看到所有端點。

---

## 2. 安全機制測試

### 2.1 未帶 API Key → 401

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST "http://localhost:8000/api/v1/source/ingest/crypto" \
  -H "Content-Type: application/json" \
  -d '{}'
```

預期：`401`

### 2.2 錯誤 API Key → 403

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST "http://localhost:8000/api/v1/source/ingest/crypto" \
  -H "X-API-Key: wrong-key" \
  -H "Content-Type: application/json" \
  -d '{}'
```

預期：`403`

### 2.3 正確 API Key → 通過認證

```bash
curl -s -o /dev/null -w "%{http_code}" \
  "http://localhost:8000/api/v1/source/datasets" \
  -H "X-API-Key: dev-source-key"
```

預期：`200`

### 2.4 限流測試

快速連續發送大量請求，確認超過限制後回傳 429：

```bash
# 預設限流：100 次/60 秒（每 API Key + IP）
# 快速發送 105 次
for i in $(seq 1 105); do
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    "http://localhost:8000/api/v1/source/datasets" \
    -H "X-API-Key: dev-source-key")
  if [ "$code" = "429" ]; then
    echo "第 $i 次請求被限流 (429)"
    break
  fi
done
```

### 2.5 IP 允許名單測試（選擇性）

若已設定 `SOURCE_ALLOWLIST_CIDRS`，從非允許 IP 發送請求應收到 403：

```json
{ "detail": "Client IP not allowlisted" }
```

---

## 3. Source API 測試

所有 Source API 請求需在 Header 帶入 `X-API-Key: dev-source-key`。

### 3.1 查詢可用 Dataset

```bash
curl "http://localhost:8000/api/v1/source/datasets" \
  -H "X-API-Key: dev-source-key"
```

預期：回傳 `success: true`，`data` 包含多個 dataset（如 `crypto_eod`、`us_stock_eod`、`fx_eod` 等）。

### 3.2 標準格式攝取（Crypto）

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/crypto" \
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

**記下 `run_id`**，後續步驟會用到。

### 3.3 去重驗證（再送一次相同 idempotency_key）

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/crypto" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  --data-binary "@scripts/sample_ingest_payload.json"
```

預期回傳相同 `run_id`，message 為 `"Duplicate idempotency_key, returning existing run"`。

### 3.4 Direct 格式攝取（US Stock）

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/usstock/direct" \
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
        "volume": 45000000,
        "name": "Apple Inc"
      }
    ]
  }'
```

### 3.4b Direct 格式攝取（Crypto）

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/crypto/direct" \
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

### 3.4c Direct 格式攝取（FX）

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/fx/direct" \
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

### 3.4d Direct 格式攝取（WTX 期貨）

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/wtx/direct" \
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

### 3.5 Direct 格式攝取（HK/China）

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/hkchina/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": { "source": "bloomberg", "query_time": "2026-03-12T08:00:00Z" },
    "data": [
      {
        "ticker": "700 HK Equity",
        "date": "2026-03-12",
        "open": 415.0,
        "high": 425.0,
        "low": 413.0,
        "close": 420.0,
        "volume": 18000000
      }
    ]
  }'
```

### 3.6 Direct 格式攝取（Macro）

```bash
curl -X POST "http://localhost:8000/api/v1/source/ingest/macro/direct" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": { "source": "Bloomberg", "query_time": "2026-03-12T08:00:00Z" },
    "data": [
      {
        "ticker": "SOFRRATE Index",
        "date": "2026-03-12",
        "value": 5.32
      },
      {
        "ticker": "USGG10YR Index",
        "date": "2026-03-12",
        "value": 4.85
      }
    ]
  }'
```

### 3.7 區域市場攝取（TW / HK / CN）

```bash
# 台灣
curl -X POST "http://localhost:8000/api/v1/source/ingest/tw" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_key": "tw_equity_eod",
    "source": "bloomberg",
    "request_key": "tw_test_001",
    "idempotency_key": "tw_test_001",
    "payload": { "metadata": {}, "data": [] },
    "fetched_at": "2026-02-23T10:00:00Z"
  }'

# 香港
curl -X POST "http://localhost:8000/api/v1/source/ingest/hk" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_key": "hk_equity_eod",
    "source": "bloomberg",
    "request_key": "hk_test_001",
    "idempotency_key": "hk_test_001",
    "payload": { "metadata": {}, "data": [] },
    "fetched_at": "2026-02-23T10:00:00Z"
  }'

# 中國
curl -X POST "http://localhost:8000/api/v1/source/ingest/cn" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_key": "cn_equity_eod",
    "source": "bloomberg",
    "request_key": "cn_test_001",
    "idempotency_key": "cn_test_001",
    "payload": { "metadata": {}, "data": [] },
    "fetched_at": "2026-02-23T10:00:00Z"
  }'
```

### 3.8 查詢批次狀態

將 §3.2 取得的 `run_id` 代入：

```bash
curl "http://localhost:8000/api/v1/source/runs/{run_id}" \
  -H "X-API-Key: dev-source-key"
```

預期 `status` 為 `completed`（或等待數秒後再查詢）。

### 3.9 重新執行正規化（Rerun）

```bash
curl -X POST "http://localhost:8000/api/v1/source/runs/{run_id}/rerun" \
  -H "X-API-Key: dev-source-key"
```

預期回傳新的 `run_id`，message 為 `"Rerun queued from raw payload {run_id}"`。

### 3.10 Market Mismatch 錯誤測試

送 CRYPTO dataset 到 US 端點，應回傳 400：

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST "http://localhost:8000/api/v1/source/ingest/us" \
  -H "X-API-Key: dev-source-key" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_key": "crypto_eod",
    "source": "bloomberg",
    "request_key": "mismatch_test",
    "idempotency_key": "mismatch_test",
    "payload": { "metadata": {}, "data": [] },
    "fetched_at": "2026-02-23T10:00:00Z"
  }'
```

預期：`400`

---

## 4. Serve API 測試

Serve API 預設不需認證（`SERVE_REQUIRE_AUTH=false`）。

### 4.1 標的查詢

```bash
# 全部標的
curl "http://localhost:8000/api/v1/serve/instruments"

# 按市場篩選
curl "http://localhost:8000/api/v1/serve/instruments?market=CRYPTO"

# 按資產類別篩選
curl "http://localhost:8000/api/v1/serve/instruments?asset_class=equity"

# 精確查詢
curl "http://localhost:8000/api/v1/serve/instruments?symbol=BTC"
```

### 4.2 單一標的

從 §4.1 取得 `instrument_id`，代入：

```bash
curl "http://localhost:8000/api/v1/serve/instruments/{instrument_id}"
```

不存在的 ID 應回傳 404。

### 4.3 日K 資料

```bash
# 按市場 + 代碼 + 日期範圍
curl "http://localhost:8000/api/v1/serve/eod?market=CRYPTO&symbols=BTC,ETH&start_date=2026-01-01&end_date=2026-01-31"

# 按單一標的 ID
curl "http://localhost:8000/api/v1/serve/eod/{instrument_id}?start_date=2026-01-01"
```

### 4.4 公司行為

```bash
# 全部公司行為
curl "http://localhost:8000/api/v1/serve/corporate-actions"

# 按市場 + 類型篩選
curl "http://localhost:8000/api/v1/serve/corporate-actions?market=US&action_type=dividend"

# 按標的 ID
curl "http://localhost:8000/api/v1/serve/corporate-actions/{instrument_id}"
```

### 4.5 宏觀經濟指標

```bash
# 列出指標序列
curl "http://localhost:8000/api/v1/serve/macro/series"

# 模糊搜尋名稱
curl "http://localhost:8000/api/v1/serve/macro/series?name=CPI"

# 列出觀測值
curl "http://localhost:8000/api/v1/serve/macro/observations"

# 按來源代碼篩選
curl "http://localhost:8000/api/v1/serve/macro/observations?source_code=CPI_YOY"

# 按序列 ID 查詢觀測值
curl "http://localhost:8000/api/v1/serve/macro/observations/{series_id}?start_date=2025-01-01"
```

### 4.6 期貨

```bash
# 列出期貨合約
curl "http://localhost:8000/api/v1/serve/futures/contracts"

# 按市場篩選
curl "http://localhost:8000/api/v1/serve/futures/contracts?market=WTX"

# 連續期貨日K
curl "http://localhost:8000/api/v1/serve/futures/continuous?symbols=WTX&start_date=2026-01-01"

# 按標的 ID 查詢連續期貨
curl "http://localhost:8000/api/v1/serve/futures/continuous/{instrument_id}"
```

### 4.7 交易日曆

```bash
# 必須指定 market
curl "http://localhost:8000/api/v1/serve/calendar?market=US&start_date=2026-01-01&end_date=2026-01-31"

# 只查休市日
curl "http://localhost:8000/api/v1/serve/calendar?market=US&is_open=false&start_date=2026-01-01&end_date=2026-12-31"
```

未帶 `market` 應回傳 422（必填參數）。

### 4.8 分頁驗證

```bash
# 第 1 頁，每頁 5 筆
curl "http://localhost:8000/api/v1/serve/instruments?page=1&page_size=5"

# 第 2 頁
curl "http://localhost:8000/api/v1/serve/instruments?page=2&page_size=5"
```

確認 `pagination.total_records` 與 `pagination.total_pages` 正確。

---

## 5. Admin API 測試

所有 Admin API 請求需在 Header 帶入 `X-API-Key: dev-admin-key`。

> 若使用 Docker 環境，需先在 `docker-compose.yml` 或 `.env` 設定 `ADMIN_API_KEYS=dev-admin-key`，然後重啟服務：
> ```bash
> docker-compose restart app
> ```

### 5.1 認證測試

```bash
# 未帶 API Key → 401
curl -s -o /dev/null -w "%{http_code}" \
  "http://localhost:8000/api/v1/admin/corrections"

# 錯誤 API Key → 403
curl -s -o /dev/null -w "%{http_code}" \
  "http://localhost:8000/api/v1/admin/corrections" \
  -H "X-API-Key: wrong-key"

# 正確 API Key → 200
curl -s -o /dev/null -w "%{http_code}" \
  "http://localhost:8000/api/v1/admin/corrections" \
  -H "X-API-Key: dev-admin-key"
```

### 5.2 修正日K 資料

先從 Serve API 取得一筆日K 的 `instrument_id`：

```bash
curl "http://localhost:8000/api/v1/serve/instruments?symbol=BTC"
```

從回應取得 `instrument_id`，再確認該日期有日K 資料：

```bash
curl "http://localhost:8000/api/v1/serve/eod?market=CRYPTO&symbols=BTC"
```

從回應記下 `instrument_id` 與某筆的 `trade_date`，然後修正收盤價：

```bash
curl -X PATCH \
  "http://localhost:8000/api/v1/admin/eod/{instrument_id}/{trade_date}" \
  -H "X-API-Key: dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{
    "correction_reason": "測試修正：調整收盤價",
    "close": 90000.00
  }'
```

預期回傳 `200 OK` 與 `correction_id`。

**驗證**：再次查詢 Serve API 確認 close 已更新：

```bash
curl "http://localhost:8000/api/v1/serve/eod/{instrument_id}?start_date={trade_date}&end_date={trade_date}"
```

### 5.3 找不到記錄 → 404

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X PATCH \
  "http://localhost:8000/api/v1/admin/eod/00000000-0000-0000-0000-000000000000/2026-01-16" \
  -H "X-API-Key: dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"correction_reason": "test"}'
```

預期：`404`

### 5.4 無變更 → 400

送與現有值完全相同的修正：

```bash
# 先查詢現有收盤價
curl "http://localhost:8000/api/v1/serve/eod/{instrument_id}?start_date={trade_date}&end_date={trade_date}"

# 送相同的值（close = 原本的值）
curl -s -o /dev/null -w "%{http_code}" \
  -X PATCH \
  "http://localhost:8000/api/v1/admin/eod/{instrument_id}/{trade_date}" \
  -H "X-API-Key: dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"correction_reason": "test", "close": <現有值>}'
```

預期：`400`

### 5.5 標記 DQ Issue 已解決

若系統中有未解決的 DQ issue，先查詢（目前需直接查 DB 或透過 Source API 攝取觸發 DQ）：

```bash
# 若有已知的 issue_id，標記為已解決
curl -X PATCH \
  "http://localhost:8000/api/v1/admin/dq-issues/{issue_id}/resolve" \
  -H "X-API-Key: dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"correction_reason": "手動驗證確認資料正確，標記 DQ issue 已解決"}'
```

預期回傳 `200 OK` 與 `resolved_at`。

**重複標記 → 409**：

```bash
# 再次標記同一個 issue → 409 Conflict
curl -s -o /dev/null -w "%{http_code}" \
  -X PATCH \
  "http://localhost:8000/api/v1/admin/dq-issues/{issue_id}/resolve" \
  -H "X-API-Key: dev-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"correction_reason": "重複標記測試"}'
```

預期：`409`

### 5.6 查詢修正紀錄

```bash
# 查詢所有修正紀錄
curl "http://localhost:8000/api/v1/admin/corrections" \
  -H "X-API-Key: dev-admin-key"

# 篩選 EOD 修正
curl "http://localhost:8000/api/v1/admin/corrections?table_name=market_data_eod" \
  -H "X-API-Key: dev-admin-key"

# 篩選特定標的
curl "http://localhost:8000/api/v1/admin/corrections?instrument_id={instrument_id}" \
  -H "X-API-Key: dev-admin-key"
```

確認：
- `corrected_by` 格式為 `xxxx****`（前 4 碼 + 遮罩）
- `before_snapshot` 與 `after_snapshot` 包含修改前後的欄位值
- 結果為**最新優先**排序

---

## 6. 端到端煙霧測試

一次跑完完整流程：健康檢查 → datasets → 攝取 → 等待完成 → 查詢結果。

```bash
#!/bin/bash
set -e
BASE="http://localhost:8000"
KEY="dev-source-key"

echo "=== 1. 健康檢查 ==="
curl -s "$BASE/health" | python3 -m json.tool

echo -e "\n=== 2. 查詢 Datasets ==="
curl -s "$BASE/api/v1/source/datasets" -H "X-API-Key: $KEY" | python3 -m json.tool

echo -e "\n=== 3. 攝取 Crypto 資料 ==="
RESULT=$(curl -s -X POST "$BASE/api/v1/source/ingest/crypto" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  --data-binary "@scripts/sample_ingest_payload.json")
echo "$RESULT" | python3 -m json.tool
RUN_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")

echo -e "\n=== 4. 等待處理完成 ==="
for i in $(seq 1 15); do
  STATUS=$(curl -s "$BASE/api/v1/source/runs/$RUN_ID" -H "X-API-Key: $KEY")
  S=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "  嘗試 $i: status=$S"
  if [ "$S" = "completed" ] || [ "$S" = "failed" ]; then break; fi
  sleep 1
done
echo "$STATUS" | python3 -m json.tool

echo -e "\n=== 5. 查詢 Instruments ==="
curl -s "$BASE/api/v1/serve/instruments?market=CRYPTO&page_size=5" | python3 -m json.tool

echo -e "\n=== 6. 查詢日K 資料 ==="
curl -s "$BASE/api/v1/serve/eod?market=CRYPTO&symbols=BTC&start_date=2026-01-01" | python3 -m json.tool

echo -e "\n=== 煙霧測試完成 ==="
```

將以上存為 `scripts/smoke_test.sh` 並執行：

```bash
chmod +x scripts/smoke_test.sh
./scripts/smoke_test.sh
```

或直接使用**視覺化測試面板**的一鍵煙霧測試按鈕（見 §7）。

---

## 7. 視覺化測試面板

瀏覽器開啟 [http://localhost:8000/test](http://localhost:8000/test)。

測試面板提供：

| 功能 | 說明 |
|------|------|
| **健康檢查** | 顯示版本、允許名單狀態 |
| **Datasets 列表** | 查詢所有可用資料集 |
| **Ingest 攝取** | 選擇市場送入測試 payload |
| **Run 狀態查詢** | 輸入 run_id 查詢處理進度 |
| **Rerun 重跑** | 以原始 payload 重新執行正規化 |
| **Instruments 查詢** | 按市場/資產類別篩選標的 |
| **EOD 查詢** | 按市場/代碼/日期查詢日K |
| **Corporate Actions** | 查詢公司行為 |
| **Macro Observations** | 查詢宏觀觀測值 |
| **Futures Continuous** | 查詢連續期貨日K |
| **一鍵煙霧測試** | 自動跑完 health → datasets → ingest → status → instruments → eod |

---

## 8. 自動化測試

### 本機

```bash
poetry run pytest
```

### Docker 容器內

```bash
docker-compose exec app bash -c \
  "TEST_DATABASE_URL=postgresql+asyncpg://findb:findb@db:5432/findb_test pytest"
```

### 測試涵蓋範圍

| 測試檔案 | 涵蓋範圍 | 數量 |
|---------|---------|------|
| `test_source_api.py` | 認證（401/403）、攝取、去重、market mismatch、allowlist、限流、生產環境 | ~22 |
| `test_serve_api.py` | instruments、eod、corporate-actions、macro、futures、calendar | ~8 |
| `test_normalize.py` | Crypto/Equity/FX 正規化邏輯 | ~5 |
| `test_usstock_normalize.py` | US Stock/Global/TW/HK/CN 正規化、區域篩選 | ~12 |
| `test_bloomberg_direct_normalize.py` | FX/Crypto/WTX/Macro Bloomberg direct 正規化 | 32 |
| `test_end_to_end.py` | 完整 ingest → normalize → query 流程 | ~3 |
| `test_admin_api.py` | Admin 認證（401/403/500）、PATCH EOD（404/400/200）、Resolve DQ（404/409/200）、audit log、分頁、corrected_by 遮罩 | 24 |
| **合計** | | **~112**（~110 passed, 2 skipped） |

### 指定執行

```bash
# 只跑 Source API 測試
poetry run pytest tests/test_source_api.py -v

# 只跑 Admin API 測試
poetry run pytest tests/test_admin_api.py -v

# 只跑 crypto 相關
poetry run pytest -k "crypto" -v

# 只跑安全機制測試
poetry run pytest -k "allowlist or rate_limit or admin_auth" -v

# 顯示覆蓋率
poetry run pytest --cov=app --cov-report=term-missing
```

---

## 9. 故障排查

| 症狀 | 原因 | 解法 |
|------|------|------|
| `Connection refused` | Docker 未啟動 | `docker-compose up -d --build`，確認 `docker-compose ps` 顯示 healthy |
| `ForeignKeyViolationError` | 未初始化種子資料 | `docker-compose exec app python /app/scripts/seed_data.py` |
| `401 Missing API key` | 未帶 X-API-Key Header | 加入 `-H "X-API-Key: dev-source-key"` |
| `403 Invalid API key` | Key 與設定不符 | 確認使用 `dev-source-key`（docker-compose 預設值） |
| `403 Client IP not allowlisted` | IP 不在允許名單 | 開發環境設定 `DEBUG=true` 或調整 `SOURCE_ALLOWLIST_CIDRS` |
| `429 Rate limit exceeded` | 請求頻率超過限制 | 等待 60 秒後重試，或調大 `RATE_LIMIT_REQUESTS` |
| `400 Dataset 'xxx' not found` | dataset_key 不存在 | 先跑 seed，或用 `/datasets` 確認可用的 key |
| `400 Market mismatch` | payload 的 dataset 市場與端點不符 | 確認 dataset_key 的 market 與端點路徑一致 |
| `422 Validation Error` | 請求體格式不符 | 檢查 JSON 欄位是否符合 schema（見 [API 使用教學](api_usage_guide.md)） |
| `.env` 設定不生效 | docker-compose 覆蓋 | `docker-compose.yml` 使用 `${VAR:-default}` 語法，`.env` 值會生效 |
| `Run status 一直 pending` | 背景任務未執行 | 確認 app 容器正常運行，檢查 `docker-compose logs app` |
| `500 No admin API keys configured` | `ADMIN_API_KEYS` 未設定 | 在 `.env` 或 `docker-compose.yml` 加入 `ADMIN_API_KEYS=your-admin-key`，重啟服務 |
| `Admin PATCH 回傳 404` | 找不到指定記錄 | 確認 instrument_id 與 trade_date 有對應的日K 資料（先用 Serve API 確認） |
| `Admin PATCH 回傳 400 No changes` | 提交值與現有值相同 | 確認修正值與 DB 現有值確實不同 |
| `Admin DQ resolve 回傳 409` | DQ issue 已解決 | 該 issue 已是 resolved 狀態，無需重複標記 |

---

## 相關文件

- **[API 使用教學](api_usage_guide.md)** — 完整端點規格、請求/回應格式、Python 範例
- [技術規格](../spec.md)
- [產品路線圖](../roadmap.md)
