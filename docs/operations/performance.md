# Performance Baselines

本文件保存可重複的staging與production量測邊界，以及何時需要優化的判斷規則。數字是
容量決策的輸入，不是production SLO，也不能用小型staging樣本universe推估production吞吐。

## Twelve Data 第一輪耗時診斷

2026-11-11 09:03–09:10（Asia/Taipei）透過 production SSM、既有 Docker logs、SQLite
`mode=ro`／`query_only` 與 PostgreSQL `READ ONLY` transaction 量測；沒有呼叫 provider、
重送 ingest、調整 controls 或更動部署。SQL 設定八秒 statement timeout。Backend 的
`full_market.py`、`delivery_plan_summary.py`、`ingestion.py`，以及 Fetcher 的
`full_market_runtime.py`、`full_market_cli.py`、`client.py`、`provider_work.py` 與當時本地
`3fed5d7` 對應檔案 SHA-256 相同。以下時間區間為 UTC，數字是當次觀察，不是固定上限。

### 實測結果

2026-11-09 交易日的 frozen cohort 為 5,547 個成員；66 個 mapping gaps，第一輪實際執行
5,481 次 provider acquisition，交付 5,276 筆有資料的單列 ingest。必須分開「抓完第一輪」
與「第一個 queue attempt 結算」，後者需要等全部 accepted runs 到 terminal。

| 階段／指標 | 觀察結果 |
| --- | --- |
| 第一個 queue attempt | 11-09 22:00:15 至 11-10 01:54:52，約 3 小時 54 分 37 秒 |
| 第一輪 acquisition 首末呼叫跨度 | 約 3 小時 14 分 44 秒；28.14 次／分鐘 |
| 最後一筆接受至最後 normalization 完成 | 01:15:04 至 01:54:50，約 39 分 46 秒 |
| Provider acquisition duration | p50 0.253 秒、p95 0.274 秒；全部累計約 23 分 28 秒 |
| 同輪相鄰 provider 呼叫起點間隔 | p50 2.118 秒、p95 3.031 秒 |
| accepted 至 normalization started | p50 75 秒、p95 336 秒、max 2,980 秒 |
| 11-10 交易日進行中的第一輪 | 3,958 次 acquisition，26.64 次／分鐘；provider p50 0.253 秒、p95 0.278 秒 |

`acquisition_intent.outcome=success` 是 invocation 層觀察，不代表 provider payload 一定有
當日資料，也不代表 canonical 成功。上表的 normalization 等待以 `normalization_job.created_at`
到 `ingestion_run.started_at` 計算；native duration 使用 `actual_start` 到 `completed_at`。

Provider catalog 的限制為 1,597 credits／分鐘，觀察吞吐約只有此值的 1.7–1.8%。
5,481 symbols 的純配額理論下限約 3.43 分鐘；這不是可承諾的完成時間，尚未計入 HTTP、
raw persistence、Source、normalization 與來源發布時間。第一輪數小時的主要成本在目前
串行 pipeline 與後端處理，不能用 no-data 重試輪的 30／60／60／120 分鐘等待解釋。

### 程式路徑與瓶頸

1. `FullMarketRuntime._deliver_members()` 逐檔執行 restore、control／Source authorization、
   native `/time_series`、同步 R2 raw persistence、prepared intent／confirm、單列 ingest、
   run-status GET，再開始下一檔。Provider HTTP 中位數僅約 0.25 秒，這些步驟的耗時累加。
2. 成功新成員的常態路徑包含約 17 次 Source RPC：prepared lookup 1 次、control poll
   3 次、普通 authorization 3 次、profile authorization 1 次、work authorize 3 次、prepare
   4 次、ingest 1 次及 run GET 1 次。`owner.reserve()` 後 `delivery.prepare()` 又 reserve；
   `delivery.prepare()` confirm 後，送出 header 時再 confirm。這些是可檢查的重複往返，
   優化仍需保存原 durable intent、confirmation、owner fence 與 dispatch freshness。
   十分鐘混合服務日誌觀察到 216 次 ingest、867 次 prepare、676 次 Twelve Data control
   poll、672 次 work authorize；包含其他 provider／背景 heartbeat，不能將所有總數當成
   Twelve Data 精確 per-member trace。131 組相鄰最後 prepare response → ingest response
   的 p50 為 1.361 秒、p95 2.072 秒；這是 response 間隔，不是 server span。
3. 一筆正常 EOD run 會刷新同一 plan summary 五次：`create_ingestion_run()` 一次，
   `execute_normalization()` 進入 processing 一次，`BaseNormalizer.process()` 的 processing
   與 completed 各一次，最後 queue terminal commit 前再一次。每次
   `refresh_plan_summary()` 取得 plan row lock，呼叫 `reconcile_plan()` 讀取全 cohort、
   canonical／delivery evidence，再查 latest run 與 completion metadata；queued／processing
   狀態尚未增加 canonical evidence，仍重做完整計數。5,276 筆成功交付的正常路徑即約
   26,380 次整份 summary refresh，工作量隨每輪成員數放大。
4. 在 5,547 成員、約 3,700 筆 data 的進行中 plan 上，純 `reconcile_plan(gap_limit=0)`
   三次耗時 0.195–0.368 秒；另外三次重現完整 summary 的 SELECT 部分為
   0.379／0.417／0.439 秒。此測法刻意排除 `FOR UPDATE`、flush、upsert 與 commit，
   不能將它當成實際寫入 span，也不能精確分攤全部 ingest latency；但足以證明逐列反覆
   全量重算是實質成本。Canonical、delivery evidence、latest run、completed time 查詢皆有
   成本，尚未用 production query plan 證實任何特定新 index 的收益。
5. Worker 配置兩個 prefork processes，但 normalization 使用 dataset-wide
   `pg_try_advisory_lock(hashtext(dataset_key))`；同一 US feed 無法真正並行。未取得鎖會建立
   新 outbox delivery，延後 5–15 秒。十分鐘觀察到 2,824 次 lock-contention deferral；
   當時只有約 58–59 個 queued jobs，仍有 job 等待超過 50 分鐘。前一天最後接受後仍花
   約 40 分鐘才排空。增加同 queue 的 worker 數不能解決此鎖粒度與重送成本。

當次 Fetcher CPU 約 2%、記憶體約 75 MiB，host 可用記憶體約 2.6 GiB；不能據此宣稱整晚
皆無資源瓶頸，但沒有支持立即升級 Fetcher 機型的證據。R2 PUT 未獨立量測，仍應補 timing。
PostgreSQL 未安裝 `pg_stat_statements`，上述 SQL 耗時來自獨立唯讀 SQLAlchemy observer。

程式碼核對另發現：診斷當時 worker 以 `process(commit=False)` 在 processing 即取得 plan
`FOR UPDATE` 並持有到 normalization commit，Source ingest 建 run 時又需同一把鎖；每筆 run
在 plan lock 內約 5 次 × 0.2–0.44 秒完整重算，與 Fetcher 每檔間隔 p50 2.118 秒及
prepare→ingest response p50 1.361 秒吻合，推論 plan lock 才是全域串行點（待量測確認）。
單次重算隨 plan 進度變慢，總成本近似 runs × cohort。Fetcher 對已 ingest、仍在 queue 的
成員每 60 秒重訪，每次再 prepare（intent + confirm）並 GET run status。Lock deferral 以
隨機 5–15 秒重送、無排序，個別 job 可能長期搶不到鎖。

### 優化順序與重新驗收

追蹤於 issue #504。

- **P0 量測（已實作）**：`Plan summary refreshed` log 記錄 `lock_wait_ms`、`reconcile_ms`、
  `total_ms`；`Canonical ingestion accepted` 記錄 `server_ms`；`Normalization settled` 記錄
  `queue_wait_ms`（job 建立至 run started）、`normalize_ms` 與 `settle_ms`。
- **P1 summary 5→1 次完整重算（已實作，待 production 重測）**：非 terminal、非
  `sequenced_snapshot` run 建立時只單調更新 latest run metadata；processing 不刷新；queue
  只在 terminal commit 前重算一次。規則見 [full_market.md](full_market.md) 的持久化完整度摘要。
- **P2 Fetcher 往返**：已有 receipt 的 normalizing 重訪只查 run status，不再 prepare、不計入
  `max_members`；再合併重複 intent／confirm，保留實際 dispatch 邊界即時授權與 durable
  prepared ACK。不要以延長 freshness 或快取 stopped 前的 grant 取代。
- **P3 鎖競爭（P1 後重測再決定）**：若 deferral 仍多，優先改為 worker 內等待
  `pg_advisory_lock` 並設 `lock_timeout`，取代隨機重送；縮小鎖粒度前須先確認 dataset 級鎖
  保護的不變式。不直接刪鎖或增加 worker 數。
- **P4** 再驗證 provider batch 與同一 frozen part 的多列交付。Twelve Data 官方支援多 symbol
  `time_series`，但 credits 仍按 symbol 計費；現有 governor 的單次單 symbol 計帳與
  adapter／journal／raw lineage 也須一起調整，不能只將 symbol 串接或提高 concurrency。
  參考[官方 batch 說明](https://support.twelvedata.com/en/articles/5203360-batch-api-requests)
  與[credits 說明](https://support.twelvedata.com/en/articles/5615854-credits)。

每階段重測完整 cohort 的 first-dispatch→last-dispatch、last-accept→last-terminal、
RPC／member、summary refresh／run、plan lock wait、queue wait p50／p95／max、lock
deferrals、R2 PUT、provider credits 與錯誤，並分前／中／後段比較，確認耗時不再隨進度上升。
停止／恢復、partial no-data、crash replay、prepared drain、DQ 與
`expected = data + no_data + missing + blocked` 必須維持。上表為 P1 前的基線，改善幅度需以
部署後的代表性完整輪次驗證。

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

先前Public staging Serve latency數字屬已移除的contract，不能當作新版基線。新版部署後以同一
region、持久TLS連線、warm-up及30 samples重新量測：instruments page 100、單商品EOD cursor page
100、單商品minute預設resolved month page 100，以及四feed各一筆evidence probe。保存p50／p95／max、
row count、image SHA、Alembic revision與query plan；這些數字仍是client-observed整體延遲，不等同SQL。

## Re-run and optimization gates

使用相同region、query shape、warm-up與sample count重測，並同時保存row count、relation size、
image SHA、Alembic revision及query plan。不得把含credential、payload或instrument資料的輸出提交。

- Raw達100,000 rows或256 MiB、sort spill到disk、metadata list SQL execution超過50 ms，或相同
  staging HTTP測法的p95連續三次比本baseline退化30%時，重測`created_at DESC`與
  `(dataset_key, created_at DESC)`候選index。只在`EXPLAIN`證明改善後用Alembic新增。
- Instrument offset page越深而明顯線性退化時，評估另行引入keyset contract；EOD與minute已使用
  opaque keyset cursor，不提高timeout或page-size上限。
- Ingest只有在代表性universe與provider cycle下p95開始逼近delivery deadline，或相同records/run
  的throughput連續三個cycle退化30%時，才比較batch insert／upsert。先保留idempotency、DQ與
  source-precedence語意，再評估吞吐差異。
- Serve先分離client-observed與server-side latency，再針對實際慢query調整index/query；不得用
  這份小型staging baseline宣告production容量。
