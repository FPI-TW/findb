# 盤中資料的儲存

盤中資料（1 分 K、逐筆成交）不放在 PostgreSQL 的行情表，而是以 Parquet 物件存在 Canonical R2；
PostgreSQL 只有索引表 `intraday_partition`。交付契約見
[`intraday_partition.v1`](ingress_contracts.md#intraday_partitionv1)。

目前只有儲存與正規化；還沒有 dataset、feed、Fetcher 抓取與 Serve 查詢（進度見
[設計文件](../dev/finlab-intraday-batch1.md)）。

## 物件

```text
intraday/v1/<dataset_key>/symbol=<代號>/year=<YYYY>/month=<MM>/day=<DD>/part.parquet   # 日檔
intraday/v1/<dataset_key>/symbol=<代號>/year=<YYYY>/month=<MM>/part.parquet            # 月檔
```

- 欄位就是契約發布的 `x-findb-row-layouts`：時間是 UTC，`trade_date` 是交易所當地日期，`symbol` 是字串，
  價格未還原，成交量單位是張（與以股為單位的日 K、舊分 K 不同）。
- 日檔內依時間（1 分 K）或當日序號（逐筆）排序；月檔依交易日再依同樣的順序。
- 同一版 Worker 對相同的資料列產生相同的位元組，重試不會寫出內容不同的物件。

## 索引表

`intraday_partition` 每個（dataset、標的、交易日）一列，只有有資料的日子才有列；當日無成交沿用官方
no-data 證據，不建立索引。

| 欄位 | 內容 |
| --- | --- |
| `row_count` | 該日的列數 |
| `source`、`source_fetched_at` | 來源與抓取時間 |
| `source_raw_sha256`、`source_rows_sha256` | provider 原始檔與交付的資料列物件各自的 SHA-256 |
| `object_layout`、`object_key`、`object_etag` | 目前保存這一天的物件是日檔或月檔、它的鍵與版本 |
| `run_id` | 寫入或確認這一天的 ingestion run |

**讀取一律先查索引。** 物件裡只取索引指派給它的交易日：月檔可能還留著某一天被取代前的資料列（該日重新
發布後索引改指向新的日檔，月檔要到下次合併才更新）。沒有索引的日期不讀檔，也不列舉 bucket。

## 正規化

`IntradayPartitionContractNormalizer` 處理一個分區：

1. 依 `rows_ref` 讀 Raw R2 的資料列物件，核對大小上限與 SHA-256。
2. 核對內容：欄位名稱、順序與型別與契約相同；列數等於 `row_count`；必填欄位沒有空值；只有該標的與該交易日；
   時間落在該交易日（交易所時區）之內；1 分 K 的時間對齊整分鐘且不重複、高低價包住開收價；逐筆的序號不重複；
   價格與數量不為負、不是無限大或 NaN；`session` 不為空。
3. 寫入日檔並更新索引，與交付證據在同一個 transaction。

| 情況 | 結果 |
| --- | --- |
| 任何一列不合規則，或資料列物件不存在、與 SHA-256 不符 | 整天不寫入，已有的日檔與索引不變；run 為 `completed_with_errors`，DQ 記錄 `INTRADAY_PARTITION_REJECTED` 與違反的規則 |
| 該日沒有索引，或送來的抓取時間不早於已保存的 | 整天替換（先前有而新檔沒有的列不會留下） |
| 兩個 SHA-256 都與已保存的相同 | 不寫物件；索引改記這次的 run，這次的交付仍算有資料 |
| 抓取時間早於已保存的那一份 | 不寫入；run 為 `completed`，DQ 記錄警告 `INTRADAY_PARTITION_STALE`，不產生交付證據 |
| R2 暫時無法讀寫 | 不算資料錯誤；由佇列重試 |

物件先寫、索引後寫。兩者之間中斷時，R2 上會有一個索引不知道的版本；重試時以實際的版本為條件覆蓋它，不會
產生重複資料，也不會留下指向不存在資料的索引。

## 月底合併

月份結束超過 `INTRADAY_MERGE_GRACE_DAYS`（預設 3 天，依交易所日曆）後，Worker 把該標的該月的日檔合併成
月檔，更新索引，再刪除日檔。合併後某一天若重新發布，那一天會再有一個日檔，下一輪再併入月檔。

- 由 Worker 主行程每 `INTRADAY_MERGE_INTERVAL_SECONDS`（預設 900 秒，設為 0 停用）執行一輪，每輪最多
  `INTRADAY_MERGE_BATCH_SIZE` 個「標的 × 月份」。要合併什麼由索引決定，沒有另外的排程或佇列狀態；一輪被
  跳過、中斷或重複執行都不影響結果。
- 正規化優先：dataset 還有等待中的 normalization 時整個 dataset 跳過；每個月份在與正規化相同的 dataset
  鎖之下合併。
- 合併前逐日核對：日檔必須是索引記錄的版本，物件裡該日的列數必須等於索引。不一致時這個月份不合併、
  不覆蓋，等待造成差異的交付完成後下一輪再試；其他月份照常進行。
- 月檔寫入後、索引提交前中斷時，日檔都還在；下一輪依索引重新合併。

## 並行與一致性

- 同一個 dataset 的正規化與合併由 PostgreSQL 的 dataset 鎖序列化，同一時間只有一個寫入者。
- 寫入仍一律帶版本條件（新建時物件不得已存在；取代時必須是先前觀察到的版本），鎖的假設被破壞時會失敗，
  不會覆蓋。
- 單一物件的寫入是原子的：讀取者看到的是取代前或取代後的完整物件。
