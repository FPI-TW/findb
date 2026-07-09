# FinDB 路線圖與進度摘要

> **最後更新**: 2026-04-08

本文件將原本偏規劃導向的 roadmap，更新為「已交付 / 進行中 / 下一步」的追蹤視角，方便快速理解目前專案實際進度。

---

## 一、目前總覽

| 區塊 | 狀態 | 說明 |
|------|------|------|
| 核心架構 | 已交付 | Source / Normalize / Serve / Admin 主流程已成形 |
| Source API | 已交付 | 標準 ingest、direct ingest、run status、rerun、dataset list 可用 |
| Serve API | 已交付 | Instruments、EOD、corporate actions、macro、futures、calendar 可查 |
| Admin API | 已交付 | EOD patch、DQ resolve、corrections、raw payload、bulk rerun 可用 |
| Phase 1 市場 | 已交付 | CRYPTO、US、FX 主流程可跑通 |
| Phase 2 區域市場 | 部分完成 | TW / HK / CN 股票與指數 normalizer 已實作，港中 direct ingest 已串接 |
| Phase 3 宏觀與期貨 | 部分完成 | Macro observation、WTX continuous futures 與 direct ingest 已實作 |
| 擴展性與維運 | 待加強 | 仍使用 app 內 BackgroundTasks、runtime schema init、offset pagination |

---

## 二、已完成項目

### 1. 共用底座

- FastAPI + SQLAlchemy async + PostgreSQL 架構已建立
- Raw / Canonical / Registry / Correction 模型已建立
- `dataset_registry`、`ingestion_run`、`dq_issue` 已納入主流程
- UUID v7、UTC、raw retention 欄位與 cleanup script 已補齊
- `scripts/seed_data.py` 已提供基礎 dataset 與 instrument 種子資料

### 2. Source API

- 市場型 ingest 端點已支援：`crypto`、`us`、`fx`、`macro`、`wtx`、`global`、`tw`、`hk`、`cn`
- direct ingest 端點已支援：
- `crypto/direct`
- `fx/direct`
- `wtx/direct`
- `macro/direct`
- `usstock/direct`
- `hkchina/direct`
- `hkchina-index/direct`
- 已支援 idempotency 去重、dataset 啟用檢查、market mismatch 檢查
- 已支援 run status 查詢與從 raw payload rerun

### 3. Normalize 與資料覆蓋

- `crypto_eod`、`crypto_index_eod`
- `us_equity_eod`、`us_index_eod`
- `fx_eod`
- `us_equity_corporate_actions`
- `macro_observation`
- `futures_contracts`、`futures_continuous_eod`
- Bloomberg direct datasets：
- `crypto_bloomberg_eod`
- `fx_bloomberg_eod`
- `wtx_bloomberg_eod`
- `macro_bloomberg_observation`
- `us_stock_eod`
- `hkchina_mixed_eod`
- `hkchina_index_eod`
- 區域 equity / index：
- `tw_equity_eod`、`hk_equity_eod`、`cn_equity_eod`
- `tw_index_eod`、`hk_index_eod`、`cn_index_eod`

### 4. Serve API

- Instruments 查詢與分頁
- EOD 查詢與單一 instrument 明細
- Corporate actions 查詢
- Macro series / observations 查詢
- Futures contracts / continuous 查詢
- Trading calendar 查詢

### 5. Admin API

- DQ issue 查詢
- DQ issue resolve
- EOD patch 與 immutable correction audit log
- Raw payload 列表與依 run 查詢
- Corrections 查詢
- Bulk rerun 既有 runs

### 6. 測試

- Source API 認證、allowlist、rate limit、ingest、rerun
- Serve API 全端點
- Admin API 認證與修正流程
- Bloomberg direct normalizer
- US / 區域市場 normalizer
- End-to-end ingest -> normalize -> serve

---

## 三、分階段進度

### Phase 0：共用底座

狀態：已完成

- Source API、Serve API、基本 DQ、dataset registry、ingestion run、raw payload 儲存已交付

### Phase 1：優先市場

狀態：已完成

- CRYPTO：EOD 與指數資料流程可用
- US：equity / index EOD、corporate actions、direct ingest 可用
- FX：EOD 與 direct ingest 可用

### Phase 2：區域股市擴展

狀態：部分完成

- TW / HK / CN equity / index normalizer 已實作
- 港中 mixed / index direct ingest 已可落到區域市場
- 待補的是更多樣本資料、完整 seed / serve 驗證與操作文件細節

### Phase 3：宏觀、債券與衍生品

狀態：部分完成

- Macro observation 與 direct ingest 已可用
- WTX continuous futures 與 direct ingest 已可用
- 債券資料集與更完整的 futures contract / roll rule 流程仍待擴充

---

## 四、目前主要技術債

- normalize 仍透過 FastAPI `BackgroundTasks` 在 API process 內觸發
- 資料表生命週期仍依賴 startup `init_db()`，尚未全面轉為 migration-first
- 大部分 list endpoint 仍使用 `count(*) + offset/limit`
- 尚未建立正式壓測基線與 worker queue 架構
- Raw retention 預設仍未啟用

---

## 五、下一步建議順序

1. 將 normalize 任務搬到獨立 worker / queue，避免 API 與大量寫入互相影響。
2. 將 bulk upsert、索引與 migration 納入正式優化計畫。
3. 補齊區域市場、macro、WTX 的樣本資料、種子資料與 smoke test。
4. 建立壓測基線與可觀測性指標，對應 `scalability_optimization_checklist.md`。

---

## 六、相關文件

見 `docs/README.md` 文件索引。
