# Staging Performance Baseline

本文件保存可重複的staging量測邊界與何時才需要優化的判斷規則。數字是容量決策的輸入，
不是production SLO，也不能用小型pilot universe推估production吞吐。

## 2026-09-15 baseline

量測使用staging既有資料，只執行唯讀SQL、`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`與
authenticated HTTPS GET；沒有建立synthetic rows或呼叫provider。

`raw.market_payload`有164 rows、約1.79 MiB。最新200筆metadata query為sequential scan＋sort，
execution 0.280 ms；`tw_equity_eod`最近30日filter為21 rows、0.128 ms；全表count使用primary-key
index-only scan，0.052 ms。現有indexes涵蓋primary key、expiry、dataset、run及idempotency scope。
在這個規模新增`created_at`或compound pagination index只有write/storage成本，沒有可證明的收益。

最近30日completed active-feed end-to-end run baseline：

| Feed | Samples | Max records/run | p50 duration | p95 duration | p50 records/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| `finlab/tw_equity_eod` | 22 | 2 | 0.048 s | 0.060 s | 41.421 |
| `shioaji/tw_equity_minute` | 22 | 270 | 1.045 s | 1.273 s | 258.360 |
| `shioaji/tw_etf_minute` | 66 | 270 | 1.039 s | 1.341 s | 259.542 |
| `twelve_data/us_equity_eod` | 54 | 10 | 0.037 s | 0.091 s | 28.064 |

Public staging Serve經同一持久TLS連線、warm-up後每個query 30 samples：

| Query | p50 | p95 | Max |
| --- | ---: | ---: | ---: |
| instruments 100 rows、`include_count=false` | 311.71 ms | 315.54 ms | 316.02 ms |
| instruments 100 rows、`include_count=true` | 323.94 ms | 341.65 ms | 341.69 ms |
| TW EOD、bounded date range、100 rows | 309.76 ms | 312.31 ms | 314.26 ms |

這是client-observed TLS／edge／network／application總延遲；不能解讀為SQL時間。`include_count`
在目前資料量沒有顯著差距。

## Re-run and optimization gates

使用相同region、query shape、warm-up與sample count重測，並同時保存row count、relation size、
image SHA、Alembic revision及query plan。不得把含credential、payload或instrument資料的輸出提交。

- Raw達100,000 rows或256 MiB、sort spill到disk、metadata list SQL execution超過50 ms，或相同
  staging HTTP測法的p95連續三次比本baseline退化30%時，重測`created_at DESC`與
  `(dataset_key, created_at DESC)`候選index。只在`EXPLAIN`證明改善後用Alembic新增。
- Offset page越深而明顯線性退化時，先提供keyset cursor並保留短期page compatibility；不要只提高
  timeout或page-size上限。
- Ingest只有在代表性universe與provider cycle下p95開始逼近delivery deadline，或相同records/run
  的throughput連續三個cycle退化30%時，才比較batch insert／upsert。先保留idempotency、DQ與
  source-precedence語意，再評估吞吐差異。
- Serve先分離client-observed與server-side latency，再針對實際慢query調整index/query；不得用
  這份小型staging baseline宣告production容量。
