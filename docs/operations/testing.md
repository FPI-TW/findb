# 測試與 PostgreSQL 隔離

`pnpm check`／`make check` 執行全 repo 的 format、lint 與 type checks，不包含測試。
`pnpm test`／`make test` 執行 contracts、Backend、Fetcher、Infra 與 Dashboard 測試；
只驗證 Backend 可用 `pnpm test:backend`／`make test-backend`，runner 會先確保 DB container 啟動。
Pre-commit 執行 check，pre-push 依序執行 check 與 test；不得使用 `--no-verify`。

`pnpm test:backend` 的非 migration tests 預設使用兩個 pytest-xdist workers 與
`--dist worksteal`，接著在同一 runner 的獨立序列 session 執行全部 `test_*migration*.py`。
`uv --directory backend run python scripts/dev.py test-db --workers 0` 可將非 migration tests
切回序列執行；migration tests 一律序列執行以重用 immutable templates。
`pnpm test:infra` 使用 Backend pytest 環境，固定 `-n 2 --dist worksteal`，不需要 PostgreSQL。
直接呼叫 `uv --directory backend run pytest` 仍預設序列執行；設定檔不注入 worker flags，
因此測試中的 nested pytest subprocess 不會意外再建立 workers。
Collection 階段產生 UUID 等非決定性值的 parameters 必須提供穩定情境 ID，避免不同
workers 的 node IDs 不同；測試輸入與 assertions 仍保留原本情境。

FinDB CI 將測試分成三個 jobs：`backend` 保留 format／lint／type checks、published contracts
與非 migration tests；`backend-migrations` 保留 Alembic smoke 與全部 migration tests；
`infra-tests` 獨立執行 `infra/tests` 且不配置 PostgreSQL service。Backend 與 Infra 固定兩個
workers；migration job 維持序列執行。Full-market schema roundtrip 位於
`backend/tests/test_full_market_migration.py`，只由 migration job 收集。
Shutdown transaction 的獨立情境由 pytest parameters 分配給 workers；每個情境使用自己的
暫存目錄與 Docker state，交易內的操作／assertions 仍在同一 case 依序執行。

三個 jobs 均產生 JUnit、含慢測試清單的 `pytest.log`、pytest `real/user/sys` 計時與
`job-time.txt`，成功或失敗都以上傳 artifact 保留 14 天。`job_elapsed_seconds` 計算第一個
step 至報告建立的時間，包含 dependencies、checks／smoke 與 tests；不含 runner／service
初始化或 artifact upload。測試輸出以 `pipefail` 捕捉，pytest 失敗仍使 reusable workflow
及 Required CI 失敗。

CI 效能驗收仍需外部實測：同 runner 規格至少三次成功執行，Backend 與 Infra 各自 job
時間中位數低於 8 分鐘，全部 jobs 的總 runner-time 相較有效基準增加不超過 20%。
比較時須使用 GitHub job 的完整 started／completed 時間，計入新增 job 的 setup、service
與 upload 成本，不能只用 pytest 或 artifact 中的 elapsed 值。失敗或缺少 runtime dependencies
的舊執行不構成有效基準；本機通過不能代替這項 CI benchmark。目前尚待遠端成功執行的證據。

Backend 的跨 application 回歸測試會直接呼叫 `fetcher/.venv/bin/python`，驗證實際 Fetcher
CLI 與部署 checkpoint 邊界。執行前須先完成 `pnpm setup`，或以
`uv --directory fetcher sync --frozen --no-dev` 建立獨立 Fetcher runtime；不需 provider extras、
AWS 或 provider credentials。FinDB CI 的 Backend job 同樣先安裝 Backend 與 Fetcher frozen
dependencies，uv cache 同時追蹤兩份 `uv.lock`，再執行跨 application 測試。

Backend 預設 `TEST_DATABASE_URL` 為
`postgresql+asyncpg://findb:findb@localhost:5435/findb_test`。
Fixture 只取此 URL 的 server、credentials 與其他連線選項，透過同 server 的 `postgres`
database 建立暫存 DB；不要求指定的基底 database 已存在，也不在其中建立、刪除或清理資料表。
測試帳號須能連線至 `postgres` 並具有 `CREATEDB` 權限，與 migration template tests 的需求相同。

一般 ORM tests 的 `test_engine` 在每個 pytest session 建立唯一的 `findb_orm_*` database，
使用 `template0` 初始化空 DB，再建立 `raw` schema 與 ORM tables。
名稱保留完整隨機 suffix，並包含有長度限制的 xdist worker token；不同 session 即使使用相同
`TEST_DATABASE_URL` 與 worker 名稱也不共享 ORM schema。初始化或測試失敗後仍執行清理，
只有成功建立的 DB 才有刪除權限；`CREATE DATABASE` 失敗不會刪除既有同名 DB。

`test_session` 的一般測試以外層 transaction 加 savepoint 保留 application commit 行為，
測試結束時 rollback。直接在測試函式參數要求 `test_engine` 的測試需要跨連線可見的 commit，
使用一般 session，結束後 truncate 所有 ORM tables。這些清理只發生於該 session 的暫存 DB。
Session teardown 先 dispose engine，再刪除自己建立的 DB；不以 shared DB 的 `drop_all`
重建 schema，也不以序列化所有 pytest runs 或重試 deadlock 掩蓋競爭。
Migration tests 維持獨立的 session templates 與 per-test clones，由原 factory 管理。

實際 PostgreSQL 隔離回歸測試可執行：

```bash
uv --directory backend run pytest tests/test_orm_database_isolation.py
```

此測試自行建立專用基底 DB，鎖住其中的 `normalization_job` 並保存 sentinel 資料與欄位，
以 PostgreSQL `lock_timeout` 有界驗證 shared `DROP TABLE` 會受既有讀鎖阻擋，且 rollback
保留該專用基底資料。
同時啟動兩個載入實際 `tests.conftest` 的 pytest subprocess。stdin/stdout barriers 確認
兩者都完成初始化與寫入後，先結束其中一個，再驗證另一個仍能讀取自己的 committed 資料。
同時驗證一般 session rollback、直接 engine 測試後的 truncate、基底 schema／資料不變，
以及各 session 暫存 DB 的 teardown。另涵蓋 schema setup failure、測試 body failure、
同名建立失敗與 PostgreSQL identifier 的長度／字元邊界。
若其中一個 subprocess 提前失敗，先取消並收完所有 stdout reader，再對所有自行建立的
probe 送 EOF 並收完輸出。EOF grace 45 秒逾時後，僅對該測試建立的 child 送 terminate，
再等 5 秒仍未結束則 kill，最後有界等待回收；各 child 獨立清理，不操作其他 process 或 DB session。
既有失敗保留原始 exception，清理資訊附加於 exception note；沒有既有失敗時，非正常清理會
令測試失敗。回歸測試涵蓋提前退出、忽略 EOF／terminate 與另一個 child 正常退出的情境。

需要實際 PostgreSQL 的測試不可用靜態檢查替代。若環境缺少 DB、權限或可用依賴，
交付時須明列未執行項目與原因。正常 fixture teardown 會清理暫存 DB；若 process 被
`SIGKILL`、清理強制結束 child 或機器中斷，仍可能留下 `findb_orm_*` DB，清理前須確認其 session 已結束，
不可依 prefix 批次刪除仍在使用中的 DB。


Staging provider functional acceptance 由 `fetcher/tests/test_staging_pilots.py`、
`backend/tests/test_staging_provider_pilots.py` 與 staging evidence probes 覆蓋。CI 必須核對 catalog 與
runtime/schema/Source scope，缺少 future provider pilot／startup acceptance 時 fail closed。
Fixture tests 與 live pipeline observations 分開；live 門檻是四個 providers／五個 feeds 各自最新 eligible 已完成
交易日的 exact pilot instrument coverage（TAIFEX 兩 actual monthly contracts／四 session rows）、同 run lineage
的 Source 202、raw、completed queue/canonical 與 Serve proof。兩日期觀測不要求歷史 backfill，也不套用
Full market enrollment/admission 與各環境實際 coverage 驗證。

Staging evidence 使用 backend unit-local `published_year` 驗證完整已發布官方年度日曆，輸出
`target_calendar_evidence` v1 的 UTC 觀測時刻、revision metadata 與最多 16 日的日期／收盤窗口；
collector 依 aware UTC 時鐘與 runtime 邊界選日（US 台北 08:15 trigger／lag 1，TW EOD／minute
14:30，TAIFEX 18:00），再檢查官方收盤。US 09:00 是交付 deadline，不是改選舊日的邊界；
minute 17:00 acquisition cutoff 後仍要求當日 proof。休市回退到官方前開市日，calendar 缺失、
不完整、revision／timezone 不符或觀測時間差超過五分鐘時 fail closed，不從最新成功 run 推導日期。
同 target 的 missing／blocked／failed 工作不能被舊 completed 遮蔽。每個 run 的 canonical
coverage 只投影一次，所有 accepted／duplicate attempt receipts 與 request SHA-256 保留；
不一致 receipts 或同日失敗 run 使 functional gate 失敗。outbox 依 run 聚合所有 delivery generation receipts，
目前狀態以唯一 normalization job 的 `delivery_id` 核對；正常 published delivery 遺失或 lease 到期後
reconciliation 的歷史 generation 不重複計算 canonical，current job 失敗或 receipts 不一致仍 fail closed。
Serve proof v1 比對 run-linked DB sample 與真實 HTTP response 的 source、`source_fetched_at`、OHLCV
及 schema-specific canonical 欄位；minute 精確核對 `bar_start_time`（單日最多取 1000 rows），
futures 核對 actual contract／session。Decimal／JSON 數值與 aware timestamps 正規化後產生 SHA-256，
collector 重新計算 projection fingerprint 並核對 sample 的 DB run provenance，匹配後才綁定 run_id。
同 key 不同來源、擷取時間或 canonical 值，以及僅宣稱 success 的 proof 都不能通過。測試以注入 UTC 時刻驗證日期邊界，
live exporter 無 CLI／環境時鐘覆寫，使用實際 UTC 時刻。

SSM collector 將 standalone exporter 原始 JSON 以 gzip/base64 傳輸；每個 stdout chunk 最多
16,000 字元，避免 `GetCommandInvocation.StandardOutputContent` 的 24,000 字元截斷。
小 payload 可 inline，大 payload 保存在同一 container 的 `/tmp/findb-staging-evidence-<nonce>.gz`
（隨機 32 位 hex nonce、exclusive create、0600），透過額外同 unit SSM commands 依序讀取。
Collector 先檢查 raw 8 MiB、compressed 2 MiB、最多 175 chunks 與 byte/count metadata，
再驗 compressed SHA-256、bounded gzip 解壓、原始長度／SHA-256，最後才解析 JSON；遺失、
重複、亂序、損毀與超預算一律拒絕。單 probe 含 chunk reads 共用 180 秒 deadline，exporter
子程序最多 120 秒且限制 stdout file size；不修改 evidence 日期、原始 receipts 或 assessment。
成功／失敗都會 best-effort 清理 nonce artifact（最多 10 秒），清理失敗不遮蔽原錯誤。
中斷留下的孤兒檔由下次 probe 僅清除專用 prefix、同 owner、regular file 且超過 24 小時的檔案；
此期限遠大於 collection deadline，不清除仍在蒐集中的檔案。測試透過本地執行完整 generated
shell wrapper 與模擬 24k SSM stdout 驗證五 feeds／兩日期及難壓縮多 chunk roundtrip、checksum、
錯誤與清理；不連 AWS，不增加 bucket、IAM 或跨 unit credentials。
