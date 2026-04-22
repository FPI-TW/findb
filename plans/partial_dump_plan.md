# Partial Dump 功能計劃與 `partial_dump.yaml` v1 欄位規格

> **建立時間**: 2026-04-22  
> **狀態**: Proposed  
> **目的**: 將遠端資料庫的「部分資料」可重複地匯出並載入本地開發環境

---

## 一、範圍與原則

1. 僅規劃，不實作程式碼。
2. 設定檔以 `configs/partial_dump.yaml` 為標準路徑。
3. 目標環境為 `prod`（可先將現有環境視作 `staging`，再切新 `prod`）。
4. 抽樣原則為「每表 3 個標的 + 最近 1 年資料」。
5. 匯出流程需可重跑、可審計、可驗證。
6. 匯入策略以 `UPSERT` 為主，盡可能重現所有 API 行為。

### 1.1 已確認輸入（2026-04-22）

1. 來源環境：`prod`。  
2. 環境策略：可先將既有環境切換為 `staging`，另開新 `prod`。  
3. 可用時段：隨時。  
4. 匯出範圍：所有表。  
5. 抽樣條件：每個市場各挑 3 個標的，取 1 年資料。  
6. 資料上限：1 年。  
7. 敏感資料：目前無。  
8. 匯入策略：`UPSERT`。  
9. 驗收方向：盡可能讓所有 API 可重現。  
10. `raw.market_payload` 納入，但最多 1000 筆。  

---

## 二、`partial_dump.yaml` v1 欄位規格（定稿）

## 2.1 Top-level

| 欄位                  | 型別          | 必填 | 預設值 | 說明                           |
| --------------------- | ------------- | ---- | ------ | ------------------------------ |
| `version`             | string        | 是   | -      | 設定版本，固定 `"1"`           |
| `profile`             | string        | 是   | -      | 設定檔名稱，例如 `dev_minimal` |
| `source`              | object        | 是   | -      | 遠端來源資料庫連線設定         |
| `target`              | object        | 是   | -      | 匯出產物輸出設定               |
| `snapshot`            | object        | 否   | 見下方 | 一致性快照與交易設定           |
| `defaults`            | object        | 否   | 見下方 | 各資料表通用預設               |
| `selection`           | object        | 否   | 見下方 | 全域抽樣規則（標的/時間窗）    |
| `table_discovery`     | object        | 否   | 見下方 | 自動發現要匯出的表（全表模式） |
| `tables`              | array<object> | 是   | -      | 要匯出的資料表清單             |
| `anonymization_rules` | object        | 否   | `{}`   | 脫敏規則 registry              |
| `load`                | object        | 否   | 見下方 | 匯入本地時的預設行為           |
| `validation`          | object        | 否   | 見下方 | 匯出/匯入後驗證規則            |
| `runtime`             | object        | 否   | 見下方 | 執行效能與容錯參數             |
| `metadata`            | object        | 否   | `{}`   | 備註、標籤、擁有者             |

## 2.2 `source`

| 欄位                   | 型別   | 必填 | 預設值               | 說明                                                       |
| ---------------------- | ------ | ---- | -------------------- | ---------------------------------------------------------- |
| `database_url_env`     | string | 是   | -                    | 讀取連線字串的環境變數名，例如 `FINDB_REMOTE_DATABASE_URL` |
| `require_ssl`          | bool   | 否   | `true`               | 是否強制 SSL 連線                                          |
| `statement_timeout_ms` | int    | 否   | `120000`             | 查詢逾時（毫秒）                                           |
| `application_name`     | string | 否   | `findb-partial-dump` | DB 連線識別名                                              |
| `readonly_required`    | bool   | 否   | `true`               | 執行前檢查連線是否唯讀角色                                 |

## 2.3 `target`

| 欄位                 | 型別   | 必填 | 預設值               | 說明                                      |
| -------------------- | ------ | ---- | -------------------- | ----------------------------------------- |
| `output_dir`         | string | 是   | -                    | 輸出根目錄，例如 `artifacts/partial_dump` |
| `artifact_name`      | string | 否   | `{profile}_{utc_ts}` | 產物名稱模板                              |
| `format`             | string | 否   | `csv`                | 目前固定 `csv`                            |
| `compress`           | string | 否   | `zstd`               | `none` / `gzip` / `zstd`                  |
| `include_schema_sql` | bool   | 否   | `true`               | 是否輸出 `schema.sql`                     |
| `include_load_sql`   | bool   | 否   | `true`               | 是否輸出 `load.sql`                       |
| `include_manifest`   | bool   | 否   | `true`               | 是否輸出 `manifest.json`                  |

## 2.4 `snapshot`

| 欄位                             | 型別   | 必填 | 預設值            | 說明                                 |
| -------------------------------- | ------ | ---- | ----------------- | ------------------------------------ |
| `enabled`                        | bool   | 否   | `true`            | 是否使用一致性快照交易               |
| `isolation_level`                | string | 否   | `repeatable_read` | `read_committed` / `repeatable_read` |
| `lock_timeout_ms`                | int    | 否   | `5000`            | lock timeout                         |
| `idle_in_transaction_timeout_ms` | int    | 否   | `60000`           | transaction 閒置逾時                 |

## 2.5 `defaults`

| 欄位         | 型別   | 必填 | 預設值  | 說明             |
| ------------ | ------ | ---- | ------- | ---------------- |
| `mode`       | string | 否   | `where` | 預設抽樣模式     |
| `where_sql`  | string | 否   | `null`  | 通用 where 條件  |
| `params`     | object | 否   | `{}`    | `where_sql` 參數 |
| `order_by`   | string | 否   | `null`  | 預設排序         |
| `limit`      | int    | 否   | `null`  | 預設筆數上限     |
| `chunk_size` | int    | 否   | `50000` | 每批匯出筆數     |

## 2.6 `selection`

| 欄位                              | 型別   | 必填 | 預設值       | 說明 |
|-----------------------------------|--------|------|--------------|------|
| `window_days`                     | int    | 否   | `365`        | 全域時間窗（最近 N 天） |
| `instrument_sample_size`          | int    | 否   | `3`          | 每表抽樣標的數 |
| `instrument_scope`                | string | 否   | `by_market`  | `by_market` / `by_table` / `global` |
| `non_instrument_table_strategy`   | string | 否   | `time_window`| 無標的欄位表一律以時間窗處理 |
| `time_columns_priority`           | array<string> | 否 | `["trade_date","obs_date","created_at","updated_at"]` | 找時間欄位優先順序 |
| `unresolved_table_policy`         | string | 否 | `discuss` | 無法判定時間欄位或標的欄位時：`discuss` / `skip` / `full` |

## 2.7 `table_discovery`

| 欄位 | 型別 | 必填 | 預設值 | 說明 |
|------|------|------|--------|------|
| `enabled` | bool | 否 | `false` | 是否啟用自動發現表 |
| `include_schemas` | array<string> | 否 | `["public"]` | 要納入的 schema |
| `exclude_tables` | array<string> | 否 | `[]` | 要排除的表（`schema.table`） |
| `require_pk_for_upsert` | bool | 否 | `true` | `UPSERT` 時要求表有 PK/UK |

## 2.8 `tables[]`

| 欄位               | 型別          | 必填 | 預設值                    | 說明                                          |
| ------------------ | ------------- | ---- | ------------------------- | --------------------------------------------- |
| `name`             | string        | 是   | -                         | 表名，不含 schema                             |
| `schema`           | string        | 否   | `public`                  | schema 名稱                                   |
| `enabled`          | bool          | 否   | `true`                    | 是否啟用該表匯出                              |
| `mode`             | string        | 否   | 承接 `defaults.mode`      | `full` / `where` / `latest_n_days` / `sample` |
| `where_sql`        | string        | 否   | 承接 `defaults.where_sql` | SQL 條件，不含 `WHERE` 關鍵字                 |
| `params`           | object        | 否   | 承接 `defaults.params`    | 查詢參數                                      |
| `order_by`         | string        | 否   | 承接 `defaults.order_by`  | 排序子句，不含 `ORDER BY`                     |
| `limit`            | int           | 否   | 承接 `defaults.limit`     | 上限筆數                                      |
| `latest_by_column` | string        | 否   | `null`                    | `latest_n_days` 模式要用的日期欄位            |
| `latest_n_days`    | int           | 否   | `null`                    | 最近 N 天                                     |
| `columns_include`  | array<string> | 否   | 全欄位                    | 僅匯出指定欄位                                |
| `columns_exclude`  | array<string> | 否   | `[]`                      | 排除指定欄位                                  |
| `depends_on`       | array<string> | 否   | `[]`                      | 依賴表（`schema.table`）                      |
| `merge_keys`       | array<string> | 否   | `[]`                      | `on_conflict=upsert` 時使用的衝突鍵欄位       |
| `anonymize`        | array<object> | 否   | `[]`                      | 欄位脫敏規則                                  |
| `post_sql`         | array<string> | 否   | `[]`                      | 表級後處理 SQL（匯入後）                      |

## 2.9 `tables[].anonymize[]`

| 欄位                | 型別   | 必填 | 預設值  | 說明                           |
| ------------------- | ------ | ---- | ------- | ------------------------------ |
| `column`            | string | 是   | -       | 欄位名稱                       |
| `rule`              | string | 是   | -       | 對應 `anonymization_rules` key |
| `nullable_fallback` | bool   | 否   | `false` | 規則失敗時是否改填 `NULL`      |

## 2.10 `anonymization_rules`

rule 定義格式：

| 欄位       | 型別   | 必填    | 說明                                                               |
| ---------- | ------ | ------- | ------------------------------------------------------------------ |
| `type`     | string | 是      | `nullify` / `static` / `hash_sha256` / `mask_email` / `mask_phone` |
| `value`    | string | 視 type | `static` 類型使用                                                  |
| `salt_env` | string | 視 type | hash 類型使用（環境變數名）                                        |

## 2.11 `load`

| 欄位                   | 型別   | 必填 | 預設值  | 說明                   |
| ---------------------- | ------ | ---- | ------- | ---------------------- |
| `truncate_before_load` | bool   | 否   | `true`  | 載入前 truncate 對應表 |
| `disable_triggers`     | bool   | 否   | `false` | 載入期間停用 trigger   |
| `verify_fk_after_load` | bool   | 否   | `true`  | 載入後做 FK 驗證       |
| `on_conflict`          | string | 否   | `upsert` | `error` / `do_nothing` / `upsert` |
| `parallel_jobs`        | int    | 否   | `1`     | 載入並行數             |

## 2.12 `validation`

| 欄位                      | 型別          | 必填 | 預設值 | 說明                 |
| ------------------------- | ------------- | ---- | ------ | -------------------- |
| `row_count_tolerance_pct` | number        | 否   | `0.0`  | 匯出與匯入比對容忍度 |
| `required_tables`         | array<string> | 否   | `[]`   | 必須存在資料的表     |
| `table_checks`            | array<object> | 否   | `[]`   | 表級檢查             |

`table_checks[]` 欄位：

| 欄位         | 型別   | 必填 | 說明               |
| ------------ | ------ | ---- | ------------------ |
| `table`      | string | 是   | `schema.table`     |
| `min_rows`   | int    | 否   | 最少筆數           |
| `max_rows`   | int    | 否   | 最多筆數           |
| `sql_assert` | string | 否   | 回傳 true 才算通過 |

## 2.13 `runtime`

| 欄位               | 型別   | 必填 | 預設值 | 說明                      |
| ------------------ | ------ | ---- | ------ | ------------------------- |
| `retries`          | int    | 否   | `2`    | 查詢重試次數              |
| `retry_backoff_ms` | int    | 否   | `1000` | 重試退避                  |
| `log_level`        | string | 否   | `info` | `debug` / `info` / `warn` |
| `fail_fast`        | bool   | 否   | `true` | 任一表失敗是否立即中止    |

## 2.14 `metadata`

| 欄位     | 型別          | 必填 | 預設值 | 說明     |
| -------- | ------------- | ---- | ------ | -------- |
| `owner`  | string        | 否   | `null` | 維護者   |
| `ticket` | string        | 否   | `null` | 任務編號 |
| `tags`   | array<string> | 否   | `[]`   | 標籤     |
| `notes`  | string        | 否   | `null` | 補充說明 |

---

## 三、欄位驗證規則（強制）

1. `version` 必須為 `"1"`。
2. `source.database_url_env` 指向的環境變數必須存在且非空。
3. `tables` 至少一筆 `enabled=true`。
4. `tables[].mode=latest_n_days` 時，`latest_by_column` 與 `latest_n_days` 必填。
5. `tables[].columns_include` 與 `tables[].columns_exclude` 不能同時非空。
6. `tables[].anonymize[].rule` 必須存在於 `anonymization_rules`。
7. `load.parallel_jobs >= 1`。
8. `target.compress` 僅允許 `none|gzip|zstd`。
9. `load.on_conflict=upsert` 時，目標表需有 PK/UK，否則 `tables[].merge_keys` 必填。
10. `selection.window_days` 固定上限 `365`（目前規劃）。
11. `table_discovery.enabled=true` 時，`tables` 可為空，實際由 discovery + overrides 決定。
12. `raw.market_payload` 若納入，必須顯式設定 `limit <= 1000`。

---

## 四、最小可用範例（MVP）

```yaml
version: "1"
profile: prod_partial_1y_3inst_all_tables

source:
  database_url_env: FINDB_REMOTE_DATABASE_URL
  require_ssl: true
  statement_timeout_ms: 120000
  application_name: findb-partial-dump
  readonly_required: true

target:
  output_dir: artifacts/partial_dump
  artifact_name: "{profile}_{utc_ts}"
  format: csv
  compress: zstd
  include_schema_sql: true
  include_load_sql: true
  include_manifest: true

snapshot:
  enabled: true
  isolation_level: repeatable_read
  lock_timeout_ms: 5000
  idle_in_transaction_timeout_ms: 60000

defaults:
  mode: where
  chunk_size: 50000

selection:
  window_days: 365
  instrument_sample_size: 3
  instrument_scope: by_market
  non_instrument_table_strategy: time_window
  unresolved_table_policy: discuss
  time_columns_priority: [trade_date, obs_date, created_at, updated_at]

table_discovery:
  enabled: true
  include_schemas: [public, raw]
  exclude_tables: []
  require_pk_for_upsert: true

tables:
  # 以下為 override；未列出的表仍由 table_discovery 納入。
  - schema: public
    name: instruments
    mode: where
    where_sql: "instrument_id IN (SELECT instrument_id FROM instruments ORDER BY instrument_id LIMIT 3)"

  - schema: raw
    name: market_payload
    mode: latest_n_days
    latest_by_column: fetched_at
    latest_n_days: 365
    limit: 1000
    # raw payload 無 PK upsert，載入時以 do_nothing
    post_sql: []

load:
  truncate_before_load: true
  verify_fk_after_load: true
  on_conflict: upsert
  parallel_jobs: 1

validation:
  row_count_tolerance_pct: 0.0
  required_tables: []
  table_checks:
    - table: public.instruments
      min_rows: 3

runtime:
  retries: 2
  retry_backoff_ms: 1000
  log_level: info
  fail_fast: true

metadata:
  owner: data-platform
  ticket: FINDB-PartialDump-Prod
  tags: [prod, partial-dump, one-year, three-instruments]
```

---

## 五、落地計劃（不含實作）

1. **Phase A - Config Contract**  
   產出設定檔 schema 驗證規格與錯誤訊息設計。

2. **Phase B - Export Plan**  
   定義匯出 SQL 產生策略、表依賴排序、快照交易控制。

3. **Phase C - Load Plan**  
   定義本地載入策略（truncate/load/verify）與失敗回滾。

4. **Phase D - Ops Plan**  
   定義唯讀憑證、排程、產物保留策略，並規劃 `prod -> staging/new prod` 切換流程。
   對無法自動判定抽樣策略的表建立討論清單（table-by-table 決策）。

5. **Phase E - CI Plan**  
   規劃 smoke dataset workflow（非 production 資料）。

---

## 六、資料格式確認狀態

1. **Schema 結構**：已知（來自 ORM、migrations、現有 API）。  
2. **實際資料分布**：待確認（例如 NULL 比率、時間欄位完整性、是否有超出 1 年窗口資料異常）。  
3. **執行前 profiling（建議）**：

```sql
-- 1) 各表筆數
SELECT table_schema, table_name, reltuples::bigint AS estimated_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname IN ('public', 'raw')
ORDER BY table_schema, table_name;

-- 2) 常見時間欄位範圍（需依實際表替換欄位）
SELECT MIN(trade_date) AS min_trade_date, MAX(trade_date) AS max_trade_date
FROM public.market_data_eod;

-- 3) instrument 維度分布
SELECT market, COUNT(*) AS cnt
FROM public.instruments
GROUP BY market
ORDER BY cnt DESC;
```

4. **確認完成門檻**：
- 每個要匯出的表都能對應到「標的抽樣或時間窗口」策略。
- 無時間欄位的表若無法套用 1 年窗口，需逐表提出討論後決策。
- `UPSERT` 目標表都有 PK/UK 或已配置 `merge_keys`。

---

## 七、完成定義（Definition of Done）

1. `partial_dump.yaml` 欄位、型別、驗證規則固定且文件化。
2. 同一份 config 重跑可得到可驗證、可重放的產物。
3. 匯出與匯入流程可在 macOS/Windows 透過 `uv` 主命令一致操作。
4. 安全邊界清楚：唯讀來源、敏感欄位可脫敏、產物不入版控。
