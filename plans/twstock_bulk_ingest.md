# Plan: TWStock 歷史資料批次匯入腳本

**目標**：寫一支 `scripts/bulk_ingest_twstock.py`，把 `/mnt/c/Users/User/Downloads/TWStock/history/{equity,index,future}/*.csv` 全部透過 `POST /api/v1/source/ingest/twstock/direct` 匯入本地 FinDB，並用 SQL 驗證資料落地。

**約束**：不修改 `app/` 既有程式碼；只新增 `scripts/` 與必要依賴。

---

## Phase 0 — Documentation Discovery（已完成，事實彙整）

### 允許的 API（Allowed APIs，皆有 file:line 來源）

**Endpoint**: `POST /api/v1/source/ingest/twstock/direct` — `app/api/v1/source.py:567`

**Auth**: Header `X-API-Key`，值需等於 `settings.SOURCE_API_KEY`（**單數！** 見 `app/config.py:30` 與 `app/api/deps.py:21`）。

**Payload schema**（`app/schemas/source.py:52-189`，原樣 copy，**不要發明欄位**）：
```json
{
  "metadata": {
    "symbol": "2330",                 // optional if every row carries symbol
    "name": null,                     // optional
    "source": "multicharts",          // default; normalizer 會 lowercase
    "file_name": "2330-Day-Trade.csv",// optional, 用於追蹤
    "query_time": null                // optional ISO datetime
  },
  "data": [
    {
      "date": "2025-05-05",           // YYYY-MM-DD（CSV 是 YYYY/M/D，要轉）
      "open": 956.0, "high": 957.0, "low": 923.0, "close": 938.0,
      "up_volume": 5249, "down_volume": 9148, "total_volume": 52710,
      "up_ticks": 2342, "down_ticks": 2369, "total_ticks": 14375
    }
  ]
}
```

**每列必填**（`app/schemas/source.py:167-189`）：`date, open, high, low, close, up_volume, down_volume, total_volume, up_ticks, down_ticks, total_ticks`；缺任一 → API 回 400。`metadata.symbol` 與 `data[].symbol` 至少要有一處。

**Response**（`app/schemas/source.py:192-198`）：`{success, run_id, status, message}`，HTTP 200 即代表 raw 已寫入並排入 normalize（asyncly）。

**寫入路徑**：endpoint → `_ingest_direct_payload` 計算 SHA256 idempotency key → 寫 `raw.market_payload` → 建 `ingestion_run` → 背景跑 `TWStockMultichartsNormalizer`（`app/services/normalize/twstock.py:9-64`）→ upsert `market_data_eod`。`asset_class="equity"`、`market="TW"` 由 normalizer **硬寫死**（`app/services/normalize/twstock.py:13-14`）。

### Anti-patterns（禁止做）

- ❌ 不要呼叫 `/api/v1/source/ingest/wtx/direct` 走期貨路徑——使用者明確要求**走台股 endpoint**，且 wtx/direct 的 schema 是 Bloomberg 格式，欄位名稱不同。
- ❌ 不要修改 `app/` 任何程式碼。
- ❌ 不要發明 `provider` 欄位——`market_data_eod` 的欄位是 `source`（`app/models/canonical.py:158`）。驗證 SQL 用 `WHERE source='multicharts'`。
- ❌ 不要在 metadata 塞不存在的欄位（schema 是 `extra="allow"` 但會被忽略），保持乾淨。

### 既有狀態（pre-flight 必須先處理）

- `.env` 裡目前是 `SOURCE_API_KEYS=...`（**複數**），但 code 讀 `SOURCE_API_KEY`（**單數**）→ 直接打 API 會 500「No API key configured」。**Phase 1 必須補一行 `SOURCE_API_KEY=...`**。
- `httpx` 在 `pyproject.toml` 是 `dev` group（`pyproject.toml:24`）；`uv` 預設 `default-groups = ["dev"]`（line 35），所以 `uv run` 會自動安裝。**不需動 pyproject.toml**。
- `tqdm` 不在依賴內。本計畫**不引入 tqdm**——改用簡單的 `print` 進度（每 10 個檔印一次），避免動 dependency。
- `python-dotenv` 已在主 dependencies（`pyproject.toml:16`）。
- `make up` 走 `scripts/dev.py up`（`Makefile:8-10`），會起 `findb-postgres` + 本地 uvicorn。

---

## Phase 1 — 前置環境檢查與修正

### 任務

1. 讀 `.env`，確認/補上：
   - `SOURCE_API_KEY=<value>` ← 用 `SOURCE_API_KEYS` 第一個值（逗號切第一個）
   - 不需要動 `SOURCE_ALLOWLIST_CIDRS`（程式碼層沒檢查；只有 nginx prod 用）
2. 確認 DB 在跑：`docker ps --filter name=findb-postgres --format '{{.Names}} {{.Status}}'`
   - 沒在跑 → `make up-db` → 等 healthcheck OK
3. 確認 Alembic head：`uv run alembic current`，必須是 `6b4b597` 對應的 head（含 `d8b9c6b4d732_add_tw_multicharts_eod_fields`）。
4. 確認 server 在跑：`curl -fsS http://localhost:8080/health`
   - 沒在跑 → `make up-server`（前景跑，或用 `nohup uv run uvicorn app.main:app --port 8080 &`）
5. 用 curl 打一發空驗證 auth 通：
   ```bash
   curl -sS -X POST http://localhost:8080/api/v1/source/ingest/twstock/direct \
     -H "X-API-Key: $SOURCE_API_KEY" -H "Content-Type: application/json" \
     -d '{"metadata":{"symbol":"TEST"},"data":[]}' | jq .
   ```
   預期 400（empty data 會被 schema/validator 擋）但**不是 401/500**。

### 驗證 checklist
- [ ] `grep -E '^SOURCE_API_KEY=' .env` 有單數版本
- [ ] `curl http://localhost:8080/health` 回 200
- [ ] curl 測試 endpoint 不是 401 也不是 500

### Anti-patterns
- 不要把 `SOURCE_API_KEY` 寫進 git-tracked 檔案。只動 `.env`（已在 .gitignore）。

---

## Phase 2 — 實作 `scripts/bulk_ingest_twstock.py`

### 檔案位置
新增單一檔：`scripts/bulk_ingest_twstock.py`，無第二個檔。

### CLI 介面
```
uv run python scripts/bulk_ingest_twstock.py \
  [--root /mnt/c/Users/User/Downloads/TWStock/history] \
  [--base-url http://localhost:8080] \
  [--api-key <key>]                # 預設讀 .env 的 SOURCE_API_KEY，再退 SOURCE_API_KEYS 第一個
  [--only equity|index|future|all] # 預設 all
  [--symbols 2330,0050]            # CSV，過濾檔名 prefix；空=全部
  [--concurrency 4]
  [--chunk-size 500]               # 每個 request 最多幾列；超過分批多 request
  [--retry 3]                      # 指數退避 0.5/1/2/4s
  [--timeout 30]                   # httpx request 秒數
  [--dry-run]                      # 只列出 plan，不打 API
  [--log-dir .ingest_log]          # 預設 repo 根的 .ingest_log/
```

### 行為規格

#### 檔案掃描
- root 下三個子目錄：`equity/`、`index/`、`future/`（後者 recursive 掃 `succeed/` `failed/` 兩層）。
- 檔案 pattern：`*-Day-Trade.csv`，`SYMBOL = filename.split("-Day-Trade.csv")[0]`。
- `--symbols` 過濾用 substring match on SYMBOL（precise equality 即可）。

#### CSV 解析
- `csv.DictReader`（header 必為 `Symbol,Date,Open,High,Low,Close,UpVolume,DownVolume,TotalVolume,UpTicks,DownTicks,TotalTicks`）。
- 日期 `YYYY/M/D` → `YYYY-MM-DD`（`datetime.strptime(s, "%Y/%m/%d").date().isoformat()`）。
- 數值轉換：OHLC → float；volume/ticks → int；空字串 → 跳過該列並記入 `skipped_rows`。
- 檔內 SYMBOL 與檔名 SYMBOL 不一致 → log warning，以**檔名**為準寫進 metadata。

#### Payload 組裝
- `metadata = {"symbol": SYMBOL, "source": "multicharts", "file_name": basename, "query_time": utcnow_iso}`
- `data = [...]`，每列 11 個必填欄位
- 列數 > `chunk_size` → 切多個 payload，每個獨立 POST（同檔多次）

#### HTTP 行為（直接 copy 這段樣式）
- 用 `httpx.AsyncClient(timeout=timeout, headers={"X-API-Key": api_key, "Content-Type": "application/json"})` 全域 reuse
- `asyncio.Semaphore(concurrency)` 限同時飛 N 個 request
- Retry：對 429 / 5xx / `httpx.TransportError` 退避 `0.5 * 2**i` 秒，最多 `--retry` 次
- 4xx (except 429) 不 retry，視為 fatal
- 每 request 計時：`time.perf_counter()` 包起來

#### Log 寫入
- `log-dir/{equity|index|future}/{SYMBOL}.json`：
  ```json
  {
    "symbol": "2330",
    "kind": "equity",
    "file": "/abs/path/2330-Day-Trade.csv",
    "rows_total": 1234,
    "rows_skipped": 0,
    "chunks": [
      {"chunk": 0, "rows": 500, "status": "ok",
       "http_status": 200, "elapsed_ms": 412.3,
       "run_id": "uuid-...", "error": null}
    ],
    "status": "ok",        // ok | partial | failed | dry-run | skipped
    "started_at": "2026-05-07T07:00:00Z",
    "finished_at": "2026-05-07T07:00:01Z"
  }
  ```
- 失敗時保留錯誤 stack trace 壓縮成 `error` 字串

#### Summary
最後印：
```
== Summary ==
total files: 130
  ok:         128
  partial:    1   (some chunks failed)
  failed:     1
  skipped:    0
total rows ingested: NNNN
elapsed: 12.3s
failed files:
  index/CAF1-Day-Trade.csv: HTTP 500 chunk 2
log dir: .ingest_log
```
exit code: 全 ok = 0；有 failed/partial = 1；dry-run = 0。

### Dry-run
- 不開 httpx
- 印每個檔的 `kind/symbol/rows/chunks` plan
- 不寫 log，只 stdout

### 程式骨架（要 copy 的結構）

```python
#!/usr/bin/env python3
"""Bulk ingest TWStock MultiCharts CSVs via Source API."""
import argparse, asyncio, csv, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from dotenv import load_dotenv

REQUIRED_COLS = [...]  # 12 cols
KINDS = ("equity", "index", "future")

def parse_args(): ...
def load_api_key(arg): ...      # arg → env SOURCE_API_KEY → env SOURCE_API_KEYS[0]
def discover_files(root, only, symbols): ...  # → list[(kind, symbol, path)]
def parse_csv(path): ...        # → (rows: list[dict], skipped: int, header_symbol: str)
def chunked(rows, n): ...
def build_payload(kind, symbol, file_name, rows): ...
async def post_chunk(client, url, payload, retry): ...
async def process_file(client, sem, ...): ...
def write_log(log_dir, kind, symbol, record): ...
def print_summary(records): ...

async def main_async(args): ...
def main(): ...
if __name__ == "__main__":
    main()
```

### 驗證 checklist (Phase 2 結束才能進 Phase 3)
- [ ] `uv run python scripts/bulk_ingest_twstock.py --help` 正常顯示
- [ ] `uv run python scripts/bulk_ingest_twstock.py --dry-run --only equity` 列出 53 檔 plan
- [ ] `ruff check scripts/bulk_ingest_twstock.py` 通過
- [ ] `black scripts/bulk_ingest_twstock.py` 已格式化

---

## Phase 3 — 單檔煙霧測試

### 任務
1. 跑單檔：
   ```bash
   uv run python scripts/bulk_ingest_twstock.py --only equity --symbols 2330
   ```
2. 確認 stdout summary `ok: 1`
3. 檢查 log：`cat .ingest_log/equity/2330.json | jq '.status, .chunks[0].http_status, .chunks[0].run_id'`
4. SQL 驗 DB：
   ```bash
   docker exec findb-postgres psql -U findb -d findb -c \
     "SELECT i.symbol, COUNT(*) AS rows FROM market_data_eod e
      JOIN instruments i ON i.instrument_id = e.instrument_id
      WHERE e.source='multicharts' AND i.symbol='2330'
      GROUP BY i.symbol;"
   ```
5. 用 run_id 確認 ingestion_run：
   ```bash
   docker exec findb-postgres psql -U findb -d findb -c \
     "SELECT status, total_records, success_records, failed_records
      FROM ingestion_run WHERE run_id='<run_id>';"
   ```

### 驗證 checklist
- [ ] HTTP 200 回應
- [ ] log 檔 `status="ok"`
- [ ] DB 有對應筆數（與 CSV 行數相當；可能受 normalize async 延遲影響，不到時 sleep 2 秒重查）
- [ ] `ingestion_run.status='completed'`、`failed_records=0`

### Anti-patterns
- 不要在 normalize 還沒跑完就斷言「沒進 DB」。retry 至少 3 次間隔 2 秒。

---

## Phase 4 — 全量匯入

### 任務
1. 全量跑：
   ```bash
   uv run python scripts/bulk_ingest_twstock.py --concurrency 4
   ```
2. 觀察 summary，確認 `ok` 數量 = `equity(53) + index(77) + future(0)` = 130
3. 失敗檔案逐一檢視 log：`cat .ingest_log/<kind>/<SYMBOL>.json | jq '.chunks[] | select(.status!="ok")'`
4. 對失敗檔重跑（idempotency 由 SHA256 保護，重打不會重複寫）：
   ```bash
   uv run python scripts/bulk_ingest_twstock.py --symbols <FAILED_SYMBOLS_CSV>
   ```

### 驗證 checklist
- [ ] summary `ok=130, failed=0, partial=0`
- [ ] `.ingest_log/` 樹完整覆蓋三個 kind

---

## Phase 5 — 最終 DB 驗證

### 任務
1. 全量 row 數：
   ```sql
   SELECT COUNT(*) FROM market_data_eod WHERE source='multicharts';
   ```
2. 每 symbol 的 row 數對比：
   ```sql
   SELECT i.symbol, COUNT(*) AS db_rows
   FROM market_data_eod e
   JOIN instruments i ON i.instrument_id = e.instrument_id
   WHERE e.source='multicharts'
   GROUP BY i.symbol
   ORDER BY i.symbol;
   ```
3. 用 shell 數 CSV row 數對齊：
   ```bash
   for f in /mnt/c/Users/User/Downloads/TWStock/history/{equity,index}/*.csv; do
     sym=$(basename "$f" -Day-Trade.csv)
     n=$(($(wc -l < "$f") - 1))   # 扣 header
     echo "$sym $n"
   done | sort > /tmp/csv_counts.txt
   ```
4. 跑一個比對腳本（可丟 awk）找出 CSV vs DB 差距 > 0 的 symbol。
5. 抽樣比對 OHLC：
   ```sql
   SELECT i.symbol, e.trade_date, e.open, e.high, e.low, e.close,
          e.up_volume, e.down_volume, e.total_ticks
   FROM market_data_eod e JOIN instruments i USING (instrument_id)
   WHERE i.symbol IN ('2330','CAF1')
   ORDER BY i.symbol, e.trade_date DESC LIMIT 10;
   ```
   人工核對和 CSV 末尾幾筆是否一致。

### 驗證 checklist
- [ ] `COUNT(*) WHERE source='multicharts'` ≥ Σ(各 CSV 行數 - skipped)
- [ ] 每個 SYMBOL 在 DB 的筆數 = CSV 有效列數（容許 ≤ 1 的差異，因 normalize DQ 可能擋極端列）
- [ ] 抽樣 OHLC 數值一致

---

## Phase 6 — 收尾

### 任務
1. `git status` 應該只看到：
   - `scripts/bulk_ingest_twstock.py` (新)
   - `plans/twstock_bulk_ingest.md` (本檔)
   - 可能 `.env`（被 .gitignore 應該不會出現，若出現要 confirm）
2. **不要 commit `.ingest_log/`**——先確認它在 `.gitignore`，沒有就加上。
3. （可選）`uv run mypy scripts/bulk_ingest_twstock.py`、`uv run ruff check scripts/`、`uv run black scripts/`

### 驗證 checklist
- [ ] `git status` 清爽，無 stray file
- [ ] `.gitignore` 含 `.ingest_log/`
- [ ] lint/format 通過

---

## 全域 Anti-patterns 集中

| 不要做 | 原因 |
|------|------|
| 動 `app/` 程式碼 | 任務明確禁止 |
| 改 `pyproject.toml` 加 tqdm | 不必要 dep；用 print 進度即可 |
| 用 `provider` 當 column name 寫 SQL | 實際欄位是 `source` (`canonical.py:158`) |
| 在 .env 把 `SOURCE_API_KEYS` 改成單數蓋掉 | 加一個 `SOURCE_API_KEY=` 即可，不要改既有 line（其他工具可能讀複數版） |
| 期貨/index 走 wtx/direct | 任務指定走 twstock/direct，雖然 normalizer 會把它們也標 `asset_class="equity"`，這是 trade-off，使用者已接受 |
| 把所有列塞同一個 request | 大檔可能逾時或被 server 拒；分 chunk 500 |
| 在 dry-run 時還寫 log | dry-run 必須完全只讀 |

---

## 執行順序

```
Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6
```

每個 phase 結束 commit 一次（或集中在 Phase 6 一次 commit，題意未要求拆分）。
