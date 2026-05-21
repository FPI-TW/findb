# 風險與錯誤原因推測清單

## A. 架構風險清單（依優先級）

### [ ] R1（高）`corporate_action` 缺業務唯一鍵，併發下可能重複入庫

- 風險描述：normalizer 採「先查再寫」，高併發時可能同時通過檢查，產生重複資料。
- 觀察訊號：同一 `instrument_id + action_type + ex_date` 出現多筆相同紀錄。
- 建議處置：新增業務唯一鍵（或以 `ON CONFLICT` upsert 取代 check-then-insert）。

### [ ] R2（高）`instrument_identifiers` 唯一鍵含 nullable `valid_from`

- 風險描述：PostgreSQL 對 `NULL` 視為不相等，可能允許多筆 `NULL`，無法真正保證 `(id_type, id_value)` 唯一。
- 觀察訊號：同一 `id_type + id_value` 多筆且 `valid_from IS NULL`。
- 建議處置：改用 partial unique index（`valid_from IS NULL`）或重整唯一鍵策略。

### [ ] R3（高）normalization 使用進程內 `BackgroundTasks`，缺 durable queue

- 風險描述：服務重啟/崩潰時，已排但未完成任務可能遺失，`run` 長時間停在 `pending`。
- 觀察訊號：`ingestion_run.status='pending'` 長時間不變。
- 建議處置：改為外部任務佇列（如 Celery/RQ/Kafka + worker + retry + dead-letter）。

### [ ] R4（中高）bootstrap migration 對部分 schema 漂移防護不足

- 風險描述：初始化僅檢查部分條件，可能出現「部分表存在、部分缺失」仍被略過。
- 觀察訊號：啟動可過，但執行到特定 query/insert 才報 relation/column 不存在。
- 建議處置：啟動時做完整 schema/revision 驗證，避免只看單一物件即 return。

### [ ] R5（中）多個 `run_id` 欄位未設 FK，lineage 無 DB 層完整性

- 風險描述：可寫入不存在的 `run_id`，追蹤鏈可能斷裂。
- 觀察訊號：canonical/raw 中出現找不到對應 `ingestion_run` 的 `run_id`。
- 建議處置：補 FK 或建立一致性檢查 job。

### [ ] R6（中）`admin/raw-payloads` 查詢可能在大數據量退化

- 風險描述：`ORDER BY created_at DESC` 在資料量大時，可能變成高成本排序/掃描。
- 觀察訊號：該 API 延遲升高、`EXPLAIN ANALYZE` 顯示排序成本高。
- 建議處置：加適配索引（例：`created_at DESC`）與分頁策略優化。

## B. 資料錯誤/缺失可能原因清單

### [ ] C1 被 rate limit 擋下（429）

- 原因：Source API 為 in-memory 限流，超過 `RATE_LIMIT_REQUESTS / RATE_LIMIT_WINDOW` 直接拒絕，不排隊。
- 影響：資料未進 ingestion。
- 檢查：API 回應碼 429、rate-limit log。

### [ ] C2 請求只完成 raw 寫入，normalize 尚未完成

- 原因：流程是「先寫 `ingestion_run + raw.market_payload` 並 commit」，再背景 normalize。
- 影響：短時間查 canonical 可能看起來「缺資料」。
- 檢查：`run.status` 是否仍為 `pending/processing`。

### [ ] C3 重複資料被 idempotency 合併

- 原因：`idempotency_key` 為 raw 主鍵，重送會回既有 run，不會重寫。
- 影響：看似「新請求沒生效」，實際被判定為重複。
- 檢查：同 `idempotency_key` 是否命中既有 `run_id`。

### [ ] C4 DB 連線池飽和或 normalize 寫入壓力過高

- 原因：normalize 為逐筆處理，非 bulk pipeline；併發大時先卡 DB pool（預設 `pool_size=5`, `max_overflow=10`）。
- 影響：延遲升高、逾時、任務堆積。
- 檢查：DB 連線數、app latency、pending run 數。

### [ ] C5 背景任務非持久化導致中斷

- 原因：BackgroundTasks 跟著 API 進程生命週期，重啟時可能中斷未完成任務。
- 影響：`run` 卡住、canonical 缺資料。
- 檢查：服務重啟時間點 vs run 狀態分布。

## C. 建議排查順序（執行清單）

### [ ] S1 先看 HTTP 分佈：`2xx / 4xx(特別是 429) / 5xx`

### [ ] S2 抽查 `ingestion_run`：`pending` 持續時間、`failed` 原因

### [ ] S3 比對 `raw.market_payload` 與 canonical 寫入落差

### [ ] S4 檢查 idempotency 命中率與重送行為

### [ ] S5 檢查 DB pool/慢查詢（含 `admin/raw-payloads`）

### [ ] S6 確認是否有服務重啟造成背景任務遺失
