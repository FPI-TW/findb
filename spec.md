# 金融資料庫技術規格文件

## 一、技術選型與基本設定

- 開發語言：Python
- 資料庫：PostgreSQL
- 時間基準：UTC
- 資料頻率：日頻（EOD）
- 主鍵策略：UUID v7
- Raw 原始資料保留：14 天

---

## 二、系統分層架構（更新版）：Fetch → Normalize → Serve

### 1) Fetch Layer（外部抓取服務｜特定設備｜不儲存）

運行位置：

- 特定設備上的外部服務

責任：

- 向資料供應商（API / 爬蟲）抓取資料
- 將抓取到的原始回傳（raw payload）呼叫主程式 API 傳入

嚴格限制（必須遵守）：

- **不得落地儲存資料**（包括 DB、檔案、長期 cache）
- 不得進行標準化 mapping
- 不得提供報告/策略/AI 使用資料
- 允許存在短暫記憶體緩衝（例如重試/批次傳送）但不得長期保存

輸出：

- 呼叫主程式 Source API，傳送 raw payload

---

### 2) Source API（主程式提供的資料入口）

用途：

- 作為 Fetch Layer 唯一資料入口
- 接收 raw payload 並落地 Raw 層（短期）與觸發/排程 Normalize

責任（主程式端）：

- 認證與授權（API Key / allowlist）
- 基礎驗證（dataset_key、request_key、idempotency_key、payload schema 最小檢查）
- 寫入 raw.market_payload（含 14 天 expire_at）
- 請求去重：以 idempotency_key 為主鍵（request_key 僅用於追蹤）
- 建立 ingestion_run 與統計
- 支援以 run_id 從 Raw 重跑（建立新的 ingestion_run 並觸發 Normalize）

---

### 3) Normalize Layer（主程式）

責任：

- 讀取 Raw payload（或由 Source API 觸發）
- 將來源欄位 mapping 為內部通用金融資料模型
- 對齊：
  - instrument（標的）
  - trading_calendar（交易日）
  - dataset 定義（dataset_registry）
- Upsert 至 Canonical 表
- 執行 Data Quality（DQ）
- 寫入 dq_issue（若有）與更新 ingestion_run

---

### 4) Serve Layer（主程式）

責任：

- 對外提供統一資料出入口（API / Query）
- 僅允許讀取 Canonical 資料表

禁止事項：

- 不得直接呼叫資料供應商 API
- 不得依賴 Raw 作業務查詢來源（Raw 僅供追溯）

---

## 三、Canonical 資料模型（摘要）

### instruments（標的主檔）

- instrument_id（UUID v7）
- asset_class（equity / index / fx / crypto / bond / future / macro）
- market（US / TW / HK / CN / FX / CRYPTO / WTX / MACRO）
- currency
- timezone
- name
- status（active / delisted）
- listed_date
- delisted_date

### instrument_identifiers（代碼映射）

- instrument_id
- id_type
- id_value
- source
- valid_from
- valid_to

### trading_calendar（交易日曆）

- market
- trade_date
- is_open
- session_open
- session_close
- holiday_name

### market_data_eod（日 K 主表）

- instrument_id
- trade_date
- open
- high
- low
- close
- volume
- turnover
- source
- asof_ts
- run_id

### corporate_action（公司行為 / 除權息）

- action_id
- instrument_id
- action_type
- ex_date
- record_date
- pay_date
- ratio
- cash_amount
- currency
- extra（jsonb）
- source
- asof_ts
- run_id

### macro_series / macro_observation

- macro_series：series_id, name, unit, frequency, market, source_code
- macro_observation：series_id, obs_date, value, source, asof_ts, run_id

### futures（台指期）

- futures_contract
- futures_continuous_eod
- roll_rule

---

## 四、Index（市場指標）技術定位（補充）

- Index 視為 instruments 中的正式標的：
  - asset_class = index
  - 使用 market_data_eod 存放日 K
- Index 不屬於 macro_series
- Dataset Registry 必須區分 equity 與 index：
  - us_equity_eod / us_index_eod
  - tw_equity_eod / tw_index_eod
  - hk_equity_eod / hk_index_eod
  - cn_equity_eod / cn_index_eod

---

## 五、Raw Layer（短期留存 14 天）

### raw.market_payload

用途：

- 追溯
- 對帳
- 除錯
- 重跑定位（短期）

欄位：

- dataset_key
- source
- request_key（上游追蹤用）
- idempotency_key（去重主鍵）
- payload（jsonb）
- fetched_at
- expire_at（fetched_at + 14 days）
- run_id

清理：

- 每日排程刪除 expire_at < now()

---

## 六、Pipeline 資料流

1. Fetch Layer 向資料來源抓取 raw payload
2. Fetch Layer 呼叫主程式 Source API 上傳 raw payload
3. 主程式寫入 raw.market_payload 並建立 ingestion_run
4. Normalize Layer 讀取 raw payload，mapping → Canonical
5. Canonical Upsert（market_data_eod / corporate_action / macro / futures）
6. 執行 DQ，寫入 dq_issue（若有）
7. Serve Layer 對外提供資料（僅讀 Canonical）
8. Raw TTL 清理（14 天）
9. （可選）透過 Source API rerun 由 Raw 重新觸發 Normalize

---

## 七、資料品質檢查（DQ）

### 日 K 基本規則

- high ≥ max(open, close)
- low ≤ min(open, close)
- volume ≥ 0
- 主鍵不可重複（instrument_id + trade_date）

### 非阻斷檢查

- 異常報酬率（例如 ±30%）
- 除權息前後價格連續性（僅對 equity）

---

## 八、下市標的策略

- instruments.status = delisted
- 記錄 delisted_date
- 保留歷史行情與公司行為
- 停止新增新交易日資料（或視需求保留最後交易日後資訊）

---

## 九、安全與可靠性（Source API 必備要求）

- 認證：API Key
- 請求去重：idempotency_key（Fetch 層生成並保證唯一）
- 重試策略：Fetch Layer 支援 retry + backoff
- 限流與允許名單：只允許特定設備/網段呼叫
- 日誌與稽核：記錄每次 ingestion_run 的來源、筆數、失敗原因
