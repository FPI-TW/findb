# FinDB 技術規格與目前實作快照

> **最後更新**: 2026-07-15

本文件保留 FinDB 的核心技術規格，並補上目前程式實際已落地的能力，避免規格與實作脫節。

---

## 一、基本設定

| 項目 | 規格 |
|------|------|
| 語言 | Python 3.13+ |
| Web Framework | FastAPI |
| ORM | SQLAlchemy 2.0 async |
| 資料庫 | PostgreSQL |
| 時間基準 | UTC |
| 主鍵策略 | UUID v7 |
| 原始資料保留 | 預設 14 天欄位設計，是否清理由 `RAW_RETENTION_ENABLED` 控制 |
| 主要資料頻率 | 以日頻（EOD）為主 |

---

## 二、系統架構

FinDB 採用 `Fetch -> Source -> durable queue -> Normalize -> Serve/Admin` 的分層方式。

```text
Fetch Layer
  -> Source API
  -> Raw Layer + ingestion_run + normalization_job + outbox (同一 DB transaction)
  -> Outbox Dispatcher -> RabbitMQ -> Celery Worker
  -> Normalize (canonical + DQ + terminal state 同一 DB transaction)
  -> Canonical Layer
  -> Serve API / Admin API
```

### Fetch Layer

- 執行於外部設備或外部服務
- 只負責抓取與轉送 raw payload
- 不應長期落地保存資料

### Source API

- 驗證 API Key、allowlist、rate limit
- 檢查 dataset / market / payload 基本結構
- 寫入 `raw.market_payload`
- 建立 `ingestion_run`、`normalization_job` 與 transactional outbox
- DB commit 成功後回 `202 Accepted`；RabbitMQ 中斷不影響 durable accept
- 支援 `run status`、`rerun`、`dataset list`

### Normalize Layer

- 依 dataset key 選擇 normalizer
- 將來源 payload mapping 為 canonical records
- upsert instrument、identifier、calendar、EOD、macro、futures、corporate action
- 執行 DQ 檢查並寫入 `dq_issue`
- Celery late ack；同 dataset 以 PostgreSQL advisory lock 串行，不同 dataset 最多並行 2 個
- RabbitMQ 只保存 delivery，raw/job/outbox 的 durable truth 位於 PostgreSQL

### Serve Layer

- 僅查詢 canonical 資料
- 不直接依賴 raw payload

### Admin Layer

- 針對 canonical 資料做人工修正與審計
- 支援 DQ resolve、EOD patch、correction audit、raw payload 查詢、bulk rerun

---

## 三、Canonical 模型摘要

### instruments

- `instrument_id`
- `asset_class`
- `market`
- `symbol`
- `name`
- `currency`
- `timezone`
- `status`
- `listed_date`
- `delisted_date`

### instrument_identifiers

- `instrument_id`
- `id_type`
- `id_value`
- `source`
- `valid_from`
- `valid_to`

### trading_calendar

- `market`
- `trade_date`
- `is_open`
- `session_open`
- `session_close`
- `holiday_name`

### market_data_eod

- `instrument_id`
- `trade_date`
- `open`
- `high`
- `low`
- `close`
- `volume`
- `turnover`
- `source`
- `asof_ts`
- `run_id`

### corporate_action

- `action_id`
- `instrument_id`
- `action_type`
- `ex_date`
- `record_date`
- `pay_date`
- `ratio`
- `cash_amount`
- `currency`
- `extra`
- `source`
- `asof_ts`
- `run_id`

### macro_series / macro_observation

- `macro_series`: `series_id`, `name`, `unit`, `frequency`, `market`, `source_code`, `source`
- `macro_observation`: `series_id`, `obs_date`, `value`, `source`, `asof_ts`, `run_id`

### futures_contract / futures_continuous_eod / roll_rule

- 支援 WTX 期貨合約、連續期貨 EOD 與 roll rule 資料結構

### correction / dq_issue / ingestion_run / normalization_job / normalization_outbox

- `dq_issue`：記錄資料品質問題與 resolve 狀態
- `correction`：記錄人工修正前後快照
- `ingestion_run`：記錄每次 ingest / rerun 的處理狀態
- `normalization_job`：記錄 execution attempt、retry、lease 與 terminal state
- `normalization_outbox`：以 publisher lease、confirm 與無上限 backoff 保證任務可補送

---

## 四、目前已實作資料集

### 已支援的標準資料集

- `crypto_eod`
- `crypto_index_eod`
- `us_equity_eod`
- `us_index_eod`
- `fx_eod`
- `us_equity_corporate_actions`
- `macro_observation`
- `futures_contracts`
- `futures_continuous_eod`
- `tw_equity_eod`
- `hk_equity_eod`
- `cn_equity_eod`
- `tw_index_eod`
- `hk_index_eod`
- `cn_index_eod`

### 已支援的 Bloomberg direct 資料集

- `crypto_bloomberg_eod`
- `fx_bloomberg_eod`
- `wtx_bloomberg_eod`
- `macro_bloomberg_observation`
- `us_stock_eod`
- `hkchina_mixed_eod`
- `hkchina_index_eod`

---

## 五、對外 API 能力

### Source API

- `POST /api/v1/source/ingest/{market}`
- `POST /api/v1/source/ingest/*/direct`
- `GET /api/v1/source/runs/{run_id}`
- `POST /api/v1/source/runs/{run_id}/rerun`
- `GET /api/v1/source/datasets`

### Serve API

- `GET /api/v1/serve/instruments`
- `GET /api/v1/serve/eod`
- `GET /api/v1/serve/corporate-actions`
- `GET /api/v1/serve/macro/series`
- `GET /api/v1/serve/macro/observations`
- `GET /api/v1/serve/futures/contracts`
- `GET /api/v1/serve/futures/continuous`
- `GET /api/v1/serve/calendar`

### Admin API

- `GET /api/v1/admin/dq-issues`
- `PATCH /api/v1/admin/eod/{instrument_id}/{trade_date}`
- `PATCH /api/v1/admin/dq-issues/{issue_id}/resolve`
- `GET /api/v1/admin/raw-payloads`
- `GET /api/v1/admin/raw-payloads/{run_id}`
- `GET /api/v1/admin/corrections`
- `POST /api/v1/admin/runs/bulk-rerun`
- `POST/GET/DELETE /api/v1/admin/source-clients`
- `GET /api/v1/admin/queue/health`

---

## 六、DQ 規則

### Blocking

- `high >= max(open, close)`
- `low <= min(open, close)`
- `volume >= 0`
- 主鍵不可重複

### Non-blocking

- 異常報酬率
- 缺漏 OHLC 欄位
- corporate action continuity 檢查

---

## 七、目前限制

- normalize 仍由 FastAPI `BackgroundTasks` 觸發，尚未拆成獨立 worker
- schema lifecycle 仍偏 runtime init，migration 佈署策略仍待補強
- Serve API 仍以 `offset/limit` 為主，未導入 cursor / keyset pagination
- 擴展性優化與壓測請參考 `scalability_optimization_checklist.md`

---

## 八、相關文件

見 `docs/README.md` 文件索引。
