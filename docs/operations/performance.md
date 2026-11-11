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

追蹤於 issue #504。P0–P3、P5 已完成，P4a 已實作；授權類 RPC 合併與連線重用經 Owner 決定暫不做。

PR #508 的 staging 驗收（2026-11-11，Twelve Data 續抓 us_equity_eod 11-09，plan data
405→1,132，727 runs）：交付 16.27→31.7 runs／分鐘，相鄰 run 間隔 p50 3.455→1.787 秒，
ingest server span p50 0.380→0.072 秒，ingest 端完整重算 0、worker 727／727 每筆一次，
summary 與完整 `reconcile_plan` 一致。可確定歸因的是 ingest 約 0.31 秒與 worker 端；間隔
多省的約 1.1 秒尚無法歸因（基線後 staging Fetcher 另部署 #474、#475）。07:28 起單次重算
隨 plan 進度變慢（reconcile p95 552 毫秒），worker 每筆開始超過進件間隔，deferral 由 0 增至
74、queue wait p95 升至 8.86 秒，因此先做 P3 與 P5。

PR #519（P3）的 staging 驗收（2026-11-11 09:01–09:21 UTC，同一 plan data 1,132→1,783，
650 runs，量測段已超過 #508 開始 deferral 的 plan 大小）：lock deferral 74→0，queue wait
p50／p95／max 0.68／1.09／1.44 秒（#508 後段 p95 8.86 秒、max 68 秒），31.6 runs／分鐘、
間隔 p50 1.649 秒，errors 0，summary 與完整 `reconcile_plan` 一致。P3 不記錄鎖等待次數，
以 deferral 與 queue wait 判定。Worker 每筆 `reconcile_ms` 仍隨進度上升：前／中／後段
p50 265／300／325 毫秒、p95 516／561／665 毫秒；`normalize_ms` p50 455 毫秒中另含
`resolve_missing_delivery_for_run` 一次未記錄的 `reconcile_plan`（gap_limit=1000）。Ingest
端 6 次完整重算全來自 Fetcher 對 mapping gap 成員回報 `blocked/mapping_gap`：
`record_outcome` 鎖內重算一次、commit 後為回應再分類一次，但 gap 成員本就算 blocked，
計數不變。

PR #520（P5）的 staging 驗收（2026-11-11 10:39–10:53 UTC，同一 plan data 1,783→2,419，
589 runs）：worker `reconcile_ms` p50／p95 300／561 → 28／45 毫秒，前後兩段 28／43、28／45
毫秒持平；`normalize_ms` p50 455→150 毫秒、`settle_ms` p50 386→71 毫秒；31.6→41.5 runs／分鐘，
間隔 p50 1.649→1.41 秒、p95 3.43→1.80 秒；queue wait p95 1.08–1.10 秒；deferral、errors 與
drift warning 皆 0。完整分類只有既有 plan 的首次初始化一次（5,551 成員 225 毫秒），ingest
outcome 改為 part（約 40 毫秒）。Summary、112 個 part 加總與完整 reconcile 一致。

PR #521（P2）的 staging 驗收（2026-11-11 11:41–11:53 UTC，455 runs，同一 plan data
2,419→2,874）：每檔 prepare RPC 4→2 次；暖機後 11:47–11:53 為 42.6 runs／分鐘、間隔 p50
1.386 秒、p95 1.68 秒（P5 同長度窗口 41.3、1.407、1.80 秒），prepare 區段（含其前的 provider
與 R2）每檔 0.704→0.633 秒。前 4 分鐘較慢（31 runs／分鐘），同時段所有未改動的 RPC 間隔也
慢 10–30%，Backend `server_ms` 與 RDS CPU（約 9–12%）不變，判定為暖機與環境波動。deferral、
errors、drift warning 皆 0，summary 與完整 reconcile 一致。當版計時 log 只在 daily batch policy
存在時輸出，部署中的 runtime 因此沒有輸出，已改為每 60 秒輸出。

同輪 ingest access log（10:48:00–10:48:30，17 筆 ingest）還原每檔約 16.5 次 Source RPC、
1.76 秒：prepared lookup 1 次；control poll 3、`full-market/authorization` 4、work authorize 3
（約 0.57 秒）；之後約 0.75 秒沒有 Source RPC（provider HTTP、R2 PUT 與本機處理）；prepare 4 次
（intent、intent、confirm、confirm，約 0.30 秒）；ingest 與 run GET 約 0.25 秒。

- **P0 量測（已實作）**：`Plan summary refreshed` log 記錄 `scope`（`plan` 為整個 cohort、
  `part` 為單一 part）、`members`（本次分類的成員數）、`lock_wait_ms`、`reconcile_ms`、
  `total_ms`；`Canonical ingestion accepted` 記錄 `server_ms`；`Normalization settled` 記錄
  `queue_wait_ms`（job 建立至 run started）、`normalize_ms` 與 `settle_ms`。API 角色
  （serve／ingest）在 FastAPI lifespan 期間以 INFO 輸出 `app.*` log，格式為
  `<UTC ISO 時間>Z <LEVEL> <logger>: <message>`；worker 沿用 Celery 的 root logging。
  先前 uvicorn 角色只輸出 WARNING 以上，`Canonical ingestion accepted` 不會出現；舊輪次的
  ingest server span 改以 `ingestion_attempt.completed_at - created_at` 近似。
- **P1 summary 5→1 次完整重算（已實作，待 production 重測）**：非 terminal、非
  `sequenced_snapshot` run 建立時只單調更新 latest run metadata；processing 不刷新；queue
  只在 terminal commit 前重算一次。規則見 [full_market.md](full_market.md) 的持久化完整度摘要。
- **P3 鎖競爭（已實作，staging 已驗收）**：拿不到 dataset 鎖時先在 PostgreSQL 鎖佇列依序
  等待最多 `NORMALIZATION_LOCK_WAIT_MS`（預設 10 秒），逾時才延後重送；取得鎖後重新讀取
  job／run，不重複處理已結算的重複 delivery。鎖與持有範圍不變。規則見
  [ingestion.md](ingestion.md)。不直接刪鎖或增加 worker 數。
- **P5 summary 增量計數（已實作，待 staging 驗收）**：`daily_delivery_part` 保存各 part 的
  計數；daily run terminal、outcome 與 historical repair 只在 plan lock 下重新分類受影響的
  part（約 50 個成員）並把差值加到摘要；outcome 回應讀已存計數，不再為回應分類整個
  cohort；完成告警檢查改用摘要加上該 part。完整分類保留給 plan 建立、sequenced snapshot、
  不可信計數的初始化與重建工具。Missing-delivery 掃描對逾期 plan 的完整分類兼作核對，
  不一致時記 `Plan summary drift corrected` 並修正。規則見 [full_market.md](full_market.md)。
  驗收：`reconcile_ms` 不再隨 plan 進度上升、log 皆為 `scope=part`、summary 與完整
  reconcile 一致。
- **P2 Fetcher 往返（已實作，staging 已驗收）**：已有 receipt 的 normalizing 重訪只查 run
  status，不再 prepare、不計入 `max_members`；同一 owner session 內相同 bytes 的 intent／confirm
  各只送一次（每檔 prepare 4→2 次），保留實際 dispatch 邊界即時授權與 durable prepared ACK，
  規則見 [provider_work.md](../architecture/provider_work.md)。不以延長 freshness 或快取 stopped
  前的 grant 取代。Fetcher 交付 plan 期間每 60 秒、交付結束時各輸出一行
  `{"delivery_timing": {"plan_id", "members", "seconds", "final", "timing"}}`；`members` 為該區間
  claim 的成員數（含只查狀態的重訪），`timing` 為 `{route 或 step: [次數, 總毫秒]}`：Source RPC
  依 route template（`{id}` 取代 UUID、不含 query），另有 `step provider_acquire`（含 R2）與
  `step raw_persist`。計時只進 log，不寫入 durable batch 或 settlement proof。部署中的 runtime
  沒有 daily batch policy（`max_members`／`max_seconds` 只在設定時生效），計時因此不依附 batch。授權類 RPC（每檔約 10 次、0.57 秒）
  是 dispatch 邊界的即時授權，是否能合併須先以這組計時拆解後另案評估。
- **P4a provider batch（已實作，待 staging 驗收）**：同一 part 的待抓成員共用一次逗號
  `/time_series`（最多 50 個、每 symbol 1 credit），R2 只存一份，仍逐成員交付；規則見
  [full_market.md](full_market.md) 的 Twelve Data 配額說明。預估每檔約 1.575 → 0.8–0.9 秒（省 provider
  HTTP、R2 PUT 與 acquisition 內的授權 RPC），Source 端每檔 prepare／ingest／run GET 不變。
- **P4b** 同一 frozen part 合併成一筆多列 ingest 可再省 Source RPC，但會改變逐成員 receipt、no-data
  重試與 prepared 語意，待 P4a 驗收後再評估。參考[官方 batch 說明](https://support.twelvedata.com/en/articles/5203360-batch-api-requests)
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
