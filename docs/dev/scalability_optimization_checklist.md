# FinDB Scalability Optimization Checklist

本文件整理 FinDB 目前為了支撐大批量資料處理與查詢所需要的優化順序，目標是作為後續開發的執行清單與追蹤依據。

適用情境：
- 單批次 ingestion 可能達到數十萬到百萬筆。
- 多個 dataset 同時進行 normalize。
- canonical table 持續成長到百萬筆以上，Serve API 仍需維持可接受延遲。

## 使用方式

- 依照 Phase 0 -> Phase 6 的順序執行，不建議跳階段。
- 每個 Phase 完成後先做壓測與回歸測試，再進下一階段。
- 若某個 Phase 只完成部分項目，請在 PR 或 issue 中明確標記未完成項。

## 目前已知瓶頸

- Source ingest 目前使用 FastAPI `BackgroundTasks` 直接在 API process 內觸發 normalize。
- normalize 主流程以逐筆 `select` / `flush` / `insert` / `upsert` 為主。
- equity 類型會逐筆查 corporate action continuity。
- Serve API list endpoint 幾乎都是 `count(*) + offset/limit`。
- raw payload 目前以整包 JSONB 落到 `raw.market_payload`。
- schema lifecycle 已改為 Alembic 版本化遷移與啟動版本檢查（不再 startup `create_all()`）。

---

## Phase 0: Baseline 與壓測基線

目標：先量到現況，不要在沒有基準的情況下優化。

- [ ] 建立固定測試資料集。
- [ ] 準備至少三組 payload 規模：`10k`、`100k`、`1M`。
- [ ] 為以下流程記錄執行時間、吞吐量與錯誤率：
- [ ] `source ingest -> normalize -> run completed`
- [ ] 單一 dataset 連續 ingest
- [ ] 多 dataset 並行 ingest
- [ ] `serve/eod`、`serve/instruments`、`serve/macro/observations` 的查詢延遲
- [ ] 記錄 PostgreSQL 指標：CPU、memory、WAL、locks、slow query
- [ ] 記錄 app 指標：單批處理時間、run queue 長度、background task 數量
- [ ] 產出 baseline 報告並保存到 `docs/dev/`

驗收條件：
- [ ] 可以明確回答目前系統在 `10k`、`100k`、`1M` 各需要多久
- [ ] 可以指出 ingest 與 query 各自最慢的 SQL 或程式區段

---

## Phase 1: 將 Normalize 從 API Process 拆出

目標：先解掉最危險的架構瓶頸，避免 API 服務與大量資料處理互相拖垮。

- [ ] 將 `BackgroundTasks` 改為外部 worker queue 架構
- [ ] 為 ingestion run 增加明確的 queueing 狀態，例如 `queued` / `processing` / `completed` / `failed`
- [ ] 將 `trigger_normalization()` 改為由 worker 消費任務
- [ ] 確保任務具備 retry 與 dead-letter 策略
- [ ] 確保服務重啟後任務不會遺失
- [ ] 為 worker 設定 concurrency 上限，避免同時塞爆 DB
- [ ] 定義單一 dataset 是否允許併發 normalize
- [ ] 補充對應文件與操作方式

建議優先修改位置：
- `app/api/v1/source.py`
- `app/api/v1/admin.py`
- `app/services/ingestion.py`

驗收條件：
- [ ] API 回應時間不再依賴 normalize 處理時間
- [ ] worker 掛掉後任務仍可重試或重新排程
- [ ] 壓測時 API worker 與 normalize worker 的資源使用可以分開觀察

---

## Phase 2: 改成 Bulk/Batch 寫入主路徑

目標：把逐筆 DB round-trip 改成批次化與 set-based 寫入，這是百萬筆等級的核心。

- [ ] 導入 staging table 作為 normalize 前的暫存層
- [ ] 大批量資料先寫入 staging table，再以 SQL 做批次 merge/upsert
- [ ] 評估使用 PostgreSQL `COPY` 或 asyncpg copy protocol 匯入 staging data
- [ ] 避免逐筆 `get_or_create_instrument`
- [ ] 先批次收集 symbols / identifiers，再一次查出既有 instrument
- [ ] 將不存在的 instrument 批次新增，而不是逐筆新增
- [ ] 將 `trading_calendar` 的建立改成按 market/date 去重後批次寫入
- [ ] 將 `MarketDataEOD`、`MacroObservation`、`FuturesContract`、`FuturesContinuousEOD` 改成批次 upsert
- [ ] 避免在每筆 upsert 後操作 session identity map
- [ ] 對大批次 run 設定 chunk size，例如 `5k`、`10k`、`20k`
- [ ] 為 chunk 處理加入明確的 metrics 與 log

建議優先修改位置：
- `app/services/normalize/base.py`
- `app/services/normalize/macro.py`
- `app/services/normalize/futures.py`
- `app/services/normalize/corporate_actions.py`

驗收條件：
- [ ] `100k` 資料寫入時間顯著低於現況
- [ ] DB round-trip 數量大幅下降
- [ ] chunk size 可透過設定調整

---

## Phase 3: 補齊資料模型與索引策略

目標：讓資料庫能支援真正的大表與高頻 upsert。

- [ ] 為 `corporate_action` 定義明確 business-key unique constraint
- [ ] 將 corporate action duplicate check 改為 DB-native upsert 或 conflict handling
- [ ] 盤點 canonical tables 的查詢與 upsert 路徑是否缺少複合索引
- [ ] 針對 `serve/eod` 常見條件確認索引是否包含：
- [ ] `instrument_id + trade_date`
- [ ] `market + symbol` 所需 join 路徑
- [ ] `macro_observation` 依 `series_id + obs_date` 的查詢排序
- [ ] `futures_continuous_eod` 依 `instrument_id + trade_date` 的查詢排序
- [ ] 評估按 `trade_date` 做 partitioning 的必要性
- [ ] 評估 raw schema 是否需要 retention job 與定期清理
- [x] 將 schema 變更正式遷移到 Alembic，而不是 runtime `create_all()`

建議優先修改位置：
- `app/models/canonical.py`
- `app/models/raw.py`
- `app/models/base.py`
- `migrations/`

驗收條件：
- [ ] 新增 constraint 與索引後，查詢計畫符合預期
- [ ] Alembic migration 可以在空庫與既有資料庫都正常執行

---

## Phase 4: 將 DQ 檢查改成批次化

目標：不要再讓 DQ 檢查成為逐筆查庫瓶頸。

- [ ] 將 corporate action continuity 改成批次查詢前值資料
- [ ] 將同一批 instrument 的歷史 EOD 一次查出，而不是逐筆 `.limit(1)`
- [ ] 將同一批 ex-date 的 corporate actions 一次查出
- [ ] 將 DQ issue 寫入改成批次 insert
- [ ] 區分 blocking DQ 與 non-blocking DQ 的處理時機
- [ ] 重新檢查百萬筆時 DQ raw_data 是否需要裁剪，避免 DQ table 膨脹

建議優先修改位置：
- `app/services/dq/validators.py`
- `app/services/normalize/base.py`

驗收條件：
- [ ] DQ 開啟後的吞吐不再比關閉時慢太多
- [ ] `equity` 類資料的 normalize 時間不會因 corporate action check 線性惡化

---

## Phase 5: 優化 Serve API 查詢模式

目標：避免 canonical table 變大後，讀取 API 因分頁策略失效。

- [ ] 將 list endpoint 從 `offset/limit` 改為 cursor 或 keyset pagination
- [ ] 將 `count(*)` 改為可選模式，或改為近似值 / 延遲計算
- [ ] 定義每個 endpoint 的預設排序鍵，確保可用於 keyset
- [ ] 針對高頻查詢加入 query plan 檢查
- [ ] 若需要 market + symbol 查詢，考慮建立對應 lookup table 或預先物化欄位
- [ ] 檢查 response model 是否有不必要欄位造成序列化成本
- [ ] 為熱門 endpoint 增加快取策略或結果快照

建議優先修改位置：
- `app/api/v1/serve.py`
- `app/schemas/serve.py`

驗收條件：
- [ ] 深翻頁不再隨頁數大幅變慢
- [ ] 大表下查詢延遲仍維持穩定

---

## Phase 6: 可觀測性、維運與回歸保護

目標：讓後續優化可被驗證，也讓退化能被提早發現。

- [ ] 為 ingestion / normalize / serve 增加 metrics
- [ ] 增加每批次的筆數、耗時、失敗率、chunk 耗時統計
- [ ] 為慢查詢建立告警
- [ ] 增加大批量整合測試
- [ ] 增加壓測腳本與固定測試資料產生器
- [ ] 在 CI 中加入至少一組中等規模資料測試
- [ ] 建立 rollback 計畫，避免資料模型優化影響既有寫入

建議新增內容：
- `tests/performance/`
- `scripts/benchmark_*.py`
- `docs/` 內的壓測報告與操作說明

驗收條件：
- [ ] 每次重大資料處理變更都能回跑 benchmark
- [ ] 可以比較優化前後吞吐量與延遲

---

## 建議實作順序總結

- [ ] 先完成 Phase 0，建立基線
- [ ] 優先完成 Phase 1，拆出 worker queue
- [ ] 接著完成 Phase 2，改成 staging + bulk write
- [ ] 再做 Phase 3，補齊 constraint / index / migration
- [ ] 然後完成 Phase 4，將 DQ 批次化
- [ ] 最後做 Phase 5 與 Phase 6，穩定 query 與 observability

## 不建議直接做的事

- [ ] 不要先微調單一 SQL 再忽略整體架構瓶頸
- [ ] 不要在 `BackgroundTasks` 架構下直接硬吃百萬筆
- [ ] 不要在沒有 benchmark 的情況下判斷優化是否有效
- [ ] 不要在未導入 migration 前直接變更 production schema

## 文件維護規則

- [ ] 每完成一個 Phase，更新本文件的完成狀態
- [ ] 每個 Phase 至少對應一個 issue 或 epic
- [ ] 若優先順序改變，請同步更新本文件與相關 roadmap
