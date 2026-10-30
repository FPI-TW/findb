# 測試與 PostgreSQL 隔離

`pnpm check`／`make check` 執行全 repo 的 format、lint 與 type checks，不包含測試。
`pnpm test`／`make test` 執行 contracts、Backend、Fetcher、Infra 與 Dashboard 測試；
只驗證 Backend 可用 `pnpm test:backend`／`make test-backend`，runner 會先確保 DB container 啟動。
Pre-commit 與 pre-push 皆只執行 check，不執行測試；不得使用 `--no-verify`。

`pnpm test:backend` 的非 migration tests 預設使用兩個 pytest-xdist workers 與
`--dist worksteal`，接著在同一 runner 的獨立序列 session 執行全部 `test_*migration*.py`。
`uv --directory backend run python scripts/dev.py test-db --workers 0` 可將非 migration tests
切回序列執行；migration tests 一律序列執行以重用 immutable templates。
`pnpm test:infra` 使用 Backend pytest 環境，固定 `-n 2 --dist worksteal --timeout=300`，
不需要 PostgreSQL。Backend pytest 設定每個測試 300 秒 timeout；卡住的測試會留下 traceback，
而不是耗盡 job timeout。
直接呼叫 `uv --directory backend run pytest` 仍預設序列執行；設定檔不注入 worker flags，
因此測試中的 nested pytest subprocess 不會意外再建立 workers。
本機比對 [FinDB CI](../../.github/workflows/findb-ci.yml)／[Fetcher CI](../../.github/workflows/fetcher-ci.yml)
時，Backend 非 migration 與 Infra suites 使用 CI 的四個 workers，migration 維持獨立序列
session；另外執行 published contract checks、全 repo check、Node.js 22／pnpm 11.15.1 下的
Dashboard unit／browser／e2e 與 build。Fetcher 使用 frozen `shioaji` extra 執行真 SDK 介面契約
測試，並建置 generic／FinLab／Shioaji／FactSet images，檢查 SDK 隔離、CLI、TLS 與唯讀 bytecode。
Backend production image 的 migration regression 使用獨立容器 DB，涵蓋 fresh／accepted-upgrade；
不在指定的本機基底 DB 直接執行 migration smoke。這些是本機 CI 同等檢查，不代表遠端 Actions 狀態。
Backend 的三個 FactSet captured-native cases 需提供 `FINDB_FACTSET_NATIVE_SNAPSHOT_DIR`
才會執行，未提供真實 HKEX／FactSet bytes 時依既定規則 skipped。兩個 ORM／migration parity
cases 必須直接通過：CHECK 名稱／缺少約束與 server defaults 均納入比對，不保留 xfail。
Infra 一般 job 的兩個 container cases 因未設
image 而 skipped，應在建置 Backend image 後以 `FINDB_TEST_BACKEND_IMAGE` 另跑並確認通過。
TAIFEX historical 只有一次 authorization dispatch attempt；prepare 與 authorize 之間跨越
UTC 分鐘時，account window 不一致會依設計 fail closed，工作延到 FIFO 尾端重試並維持
queued。`RuntimeHarness.run()` 因此在距下一分鐘不足 5 秒時先等到下一分鐘再開始 TAIFEX
run；不得以放寬產品授權期限或 window 檢查來處理此類間歇失敗。須保留並回報完整平行 run
的失敗，不能把單獨重跑成功描述成原完整 run 通過。
Fetcher 的 slow owner RPC／dispatch grant 測試使用局部可控的 wall／monotonic clock，
分別涵蓋分鐘內及跨 UTC 分鐘。跨分鐘可能合法重取 grant，但 native dispatch、actual-start
與帳號日配額只能記一次；不能將 grant 次數固定為一，也不應放寬產品的時效或 quota window 檢查。
可控時鐘不修改全域 time、asyncio、IPC poll／join 或背景 heartbeat 的真實等待行為。
`test_actual_processes_merge_proven_debt_exactly_once` 同樣將 seeded quota 與父程序 admission
固定在相同 UTC 分鐘，三個 identity merge 子程序仍以真實 spawn 併發執行；避免啟動成本跨分鐘
後，合法的新分鐘額度使舊債務阻擋斷言失效。產品的 quota rollover 規則維持不變。
Backend 的 `daily_runtime_clock` fixture 預設將日常 policy 時鐘固定在台北當日 20:00，
讓一般 admission／queue／migration 測試不依賴執行時是否落在 Shioaji 18:00–隔日 06:00 窗口。
Queue 即使收到獨立 lease clock，policy 判定仍使用相同的受控時鐘；一般案例的 UTC grant、
quota 與 lease 保持原有時鐘，Shioaji native 功能驗收則使用下述共用邏輯 UTC。
窗口邊界案例應明確調整 fixture clock 或覆寫
`daily_runtime.utc_now`／`daily_window`；pure policy tests 可直接傳入 aware datetime，
驗證開窗、關窗與跨日，不把窗口判定改成永遠允許。
`RuntimeHarness` 的 Shioaji 功能驗收預設使用 `shioaji_dispatch_clock`：Backend 的標準
`utc_now` aliases、Source checked-at／lease、Fetcher queue、account governor、SDK child 及
historical coordinator／SQLite checkpoint 共用邏輯 UTC；明確覆寫的時間邊界 clock 保留。
UTC 起點位於當下分鐘的第 30 秒，避免真實分鐘 rollover；pacing sleep 推進邏輯 UTC，
FIFO 重試的 `offset += 61` 同時釋放 account 與 checkpoint 的 rolling window。
Grant monotonic clock 在功能案例中受控，因此 ASGI／DB 排程延遲不會意外觸發 50ms 到期。
實際 Source 認證、scope、fence、header/body 一致性及 expiry 判定仍執行，沒有放寬產品限制。
功能 fake SDK 沿用 runtime 的 30 秒 IPC 上限，不使用任意縮短的 2 秒上限。

`test_shioaji_historical_functional.py` 注入第一檔／最後一檔 acquisition 失敗，驗證原始
50-member manifest、FIFO receipts、canonical evidence 與 snapshot 重用，並驗證逐檔診斷不含
provider 訊息或 credentials。Heartbeat 重疊案例用事件強制與 delivery 同時進入，不等待
真實 20 秒 interval；harness 僅序列化共用測試 AsyncSession 的 HTTP／normalization 使用，
保留 request 前的故障 barriers、真實 heartbeat thread 與正式程式的並行行為。
`test_shioaji_time_limits.py` 以顯式 clock 獨立驗證 50ms grant
及 50 次／60 秒 rolling limit；日常 18:00–06:00 窗口、31 天日期限制／台北跨日與 SDK timeout
分別由 `test_shioaji_daily_runtime.py`、`test_historical_backfill.py` 與 Fetcher 的
`test_shioaji.py`／`test_provider_process_reap.py`／`test_provider_worker.py` 驗證。
時間限制案例不依賴跑測試當下的時刻。

Shioaji 每輪輸出逐檔 `code`／`stage`／`retryable`、checkpoint attempts／status／terminal code
及安全的 attempt history；即使 coordinator 拋出例外，也保存安全的 exception type、Source
失敗路徑／status 與 checkpoint。不輸出 provider exception text、raw payload、Source body 或 credentials。
Captured warning 保存在失敗的 `pytest.log`，不受 assertion repr 截短影響。
`FINDB_TEST_DIAGNOSTICS_DIR` 可指定 JSONL 目錄；CI 設為
`$RUNNER_TEMP/findb-ci-backend/diagnostics`，隨既有 Backend artifact 成功或失敗均保存 14 天。
每個 harness 使用獨立 UUID 檔名供 xdist 並行；本機未設定時寫入該測試自己的暫存目錄。
Collection 階段產生 UUID 等非決定性值的 parameters 必須提供穩定情境 ID，避免不同
workers 的 node IDs 不同；測試輸入與 assertions 仍保留原本情境。

## Provider-work 退役驗證

Shioaji 空 Kbars／mapping 修復由 Fetcher `test_shioaji_recovery.py`、
`test_shioaji_repair_cli.py` 及 Backend `test_shioaji_repair.py`、
`test_shioaji_repair_migration.py` 驗證：legacy 快取證據保留、跨重啟三次觀測上限與十五分鐘退避、
unknown dispatch 不退款、日曆衝突、Owner／Source fence、stale preview、mapping overlay、
首次未知派送跨重啟的上限與退避、空回應後 calendar-status outage 的續報與 claim 保留、
正式 gateway／decoder 的缺欄、向量長度與 scalar 格式拒絕沿用八次一般失敗政策，
以及 Mapping API 與 Fetcher symbol validator 的邊界一致性及核准前 `422` 拒絕、
舊八次失敗及新 grant 分離、重複安裝／ACK-loss，以及 migration 保留性與 rollback floor。
Admin 修復回應另驗證 OpenAPI typed schemas、必要摘要欄位與格式、序列化後摘要相符、
使用 preview 回應套用與重播，以及錯誤 apply 回應不得提交授權。
觀測 gate 測試亦涵蓋舊空快取等待／耗盡、calendar conflict／outage／無效證據保留原 gateway，
其他成員可重用 session；SDK 建立／登入／兩個 contracts 群組失敗仍採八次一般重試，
不扣 Kbars 觀測額度，保留先前 unknown debt，後續資料不需額外 grant。
`test_sdk_bootstrap.py` 另以正式 child protocol 與離線 SDK 驗證登入失敗仍保留 native／登入帳務，
僅將 Kbars probe 記為 `predispatch`；dead child 仍須回收並由後續 acquisition 替換。
修復續作另覆蓋 persisted summary 同交易更新／回滾／重播、無 outcome 的已修復 mapping
不再顯示 gap、獨立 source error 保留，以及 repair→claim 的窗口／control／Full flag 矩陣與
Pilot 例外。本機 no-trade complete、Backend resolved、prepared／receipt 成員不得重開或新增
安裝 ACK；混合成員僅 ACK 實際安裝者，local→grant 中斷與 ACK-loss 可用同 ID 續作。
已 ACK 修復的後續回合不再查 diagnostics／restore／ACK，只補報待安裝成員自身的 outcome；
observation guard 重用 fresh member authorization，逾時才重新 RPC。
`test_provider_work_daily_faults.py` 的股票／ETF 空回應案例走正式 decorator、隔離 fake SDK、
Source→raw→normalization→canonical，驗證後續合法回應完成原 scope；同 suite 保留 outage／
lost-ACK 原 bytes／idempotency 驗證。測試不呼叫真實 provider，不能代替 production 根因驗收。

所有五 provider 的設定／無 owner 拒絕矩陣在 Backend `test_provider_work_retirement.py`
與 Fetcher `test_twelve_data_cli.py`；後者覆蓋八個舊 acquiring CLI 的 exit 2、無新檔案與原
checkpoint bytes 不變，以及 daily cycle／drain 只呼叫 queue dispatcher。部署的 false 拒絕與
bootstrap shared account path 分別由 Infra `test_provider_work_retirement.py`、
`test_aws_configuration.py`、`test_full_market_deployment_repairs.py` 覆蓋。

`test_retired_cli.py` 另外驗證 console／`python -m` 的穩定 tombstone、模組只定義 `main`，
且在 package 既有 public exports 載入後不再匯入其它 provider／legacy 模組。
舊 CLI parser、summary、私人 smoke IPC 與 TAIFEX standalone executor tests 已移除；
實際隔離 worker／FD／timeout／輸出抑制仍由現行 provider process／SDK suites 驗證。
`test_legacy_checkpoints.py` 使用舊格式資料驗證 TAIFEX 唯讀 parser，核對原 inode、permissions、
prepared、receipt 與 request／attempt 用量均保留，缺檔、錯誤 schema／identity、symlink fail closed。
WAL fixtures 同時覆蓋已關閉且無 sidecar，以及仍有未 checkpoint 證據的 sidecars，均須在
connect 前明確拒絕且不新增或改動任何原檔；不得以忽略 WAL 的 immutable reader 偽裝支援。
`test_runtime_owner_contract.py` 直接驗證真實空 claim 的 idle、缺少 owner 時不能動 retained state，
以及 member executor 使用已驗證 owner 依序 restore／authorize／reserve，沒有 owner 可選 fallback。
`test_historical_owner_contract.py` 覆蓋歷史 executor 的 stopped scope、drain-only、closed／draining、
延期、suspension 與 Source retry／fence，驗證都使用 `require_owner()` 回傳的同一 session。
空 item 可 idle，缺少 owner 則必須在 claim 前拒絕；Source fence 不得一般化成失敗或釋放 owner。

`test_runtime_image_defaults.py` 讀取 provider Dockerfile 的實際 CMD 並執行，不以另一個
CLI 覆蓋命令。CI 對 FinLab／Shioaji／FactSet 也使用無參數 `docker run`，確保預設入口可用。
`test_runtime_initialization.py` 在同 provider 的真實 flock 已被持有時啟動獨立 CLI，核對另一
profile checkpoint 可初始化、inode／原 bytes 不變且原 owner 仍排他；symlink fail closed。
Infra preflight regression 同時確認初始化不能令 held lock 通過 attestation。
Account unit decorator 以 worker 明確傳入的 `path` 決定 lock 根目錄，進入 worker 前設定
對應 ledger environment；不使用平台專屬 tmp fallback。Spawn 測試覆蓋父程序未設定或帶錯 ledger
的情況，SDK／FD inheritance 測試仍執行。

`test_source_recovery.py` 以 MockTransport 核對兩個 control clients 對 408／429／5xx
（含 521）的 bounded retries、永久錯誤拒絕與安全日誌；直接執行現行 Pilot／Full CLI
的五 provider 啟動，以及 drain／poll／gate／cycle／completion／error-report 中斷恢復。
實際 subprocess 的 SIGTERM／SIGINT 測試覆蓋內層 Retry-After 與外層 outage wait；
active work 的 bounded backoff 不因停止訊號跳過 drain。額度測試分別中斷 initial owner、
governed gate、Source authorization 與 dispatch owner recheck，並驗證實際 acquisition
已派送後保留用量／attempt；十二次中斷跨 checkpoint reopen 仍保留原 failure budget。
既有 prepared／receipts、work budget 與 quota windows 不重設，Source outage 不誤報 SDK error。
`test_source_wait_budgets.py` 另覆蓋 historical／maintenance、FactSet、TAIFEX 與 Shioaji
各自帳本的派送前後分界；十二次 Shioaji SOURCE_WAIT 不耗 failure budget／rolling allowance。
Historical control 的 transport cause 與缺少 status 的 protocol error 亦分開驗證。

`test_full_market_error_outcomes.py` 覆蓋一般 provider／transport 錯誤的 Source outcome、
第一個失敗後停止本輪，以及最後一次 attempt 的回報失敗／重啟／drain-only continuation；
必須先補報、保留原 attempts／prepared／receipts，不能再抓取下一 member。
Backend `test_provider_work_error_reports.py` 以真實 Source／claim／settle 覆蓋 reporter 503
與 lost ACK，且在 settlement 前施加 stopped／window／cooldown，驗證短 report backoff、
唯讀 projection、drain-only claim、acquisition 拒絕、冪等 outcome、原 cooldown 保留，以及 ACK
後移除補報例外、其他 eligible work 能前進。Malformed／重複／越界 member 或錯誤 kind 的
補報 proof 必須拒絕。TAIFEX snapshot decorator 與函式本體間的 stop／window 競態不回報
來源錯誤，也不消耗 acquisition failure budget。
Shioaji coordinator 的 attempt ceiling 在 governor、SDK、一般與 dispatched quota 例外都使用
同一 `terminal_on_exhaustion`：預設 staging 為 false，耗盡後維持 `ATTEMPT_BLOCKED`；
production subclass 為 true，最後一次失敗即保存 `ACQUISITION_ATTEMPTS_EXHAUSTED`。
Predispatch quota wait 不耗 budget，也不能轉 terminal。設定測試同時核對 Backend／Fetcher
truthy 解析、false 與 invalid boolean 診斷，且 owner／descriptor 檢查不能被任一 truthy 值繞過。

原 compatibility cycle／獨立 lease orchestration 測試由正式 FIFO lifecycle／native adapter
integration 測試取代；不保留已退役 CLI 的成功抓取預期。跨環境／profile 的 frozen membership
仍由 `test_execution_profile_parity.py` 直接驗證原 plan identity，calendar／normalizer 測試沿用。
Backend `test_provider_work*` 與 `test_shioaji_historical_functional.py` 使用真實 DB fence、Source、
prepared intent／confirm、receipts、normalization、canonical／Serve 與 native subprocess 替身，
涵蓋 crash/recovery、cancel/expiry drain、429/503、完整 sequence proof 與 durable retry budget。
Migration tests 只在建立 pre-queue schema fixture 時模擬舊 acceptance；升級後 replay 使用正式 fence，
核對 raw hash／prepared bodies／receipt lineage／部分 sequence checkpoint 保留。

一般 payload-focused tests 明確使用 `owned_client`／`OwnedHistoricalClient` 建立隔離的 owned scope；
protocol rejection tests 保留 raw client。Account governor／SDK／mapper layer unit tests以 fixture
替換獨立測試過的 ownership boundary，仍執行真實 account locks、pacing、quota、dispatch 與子程序回收。
這些 layer stubs 不設定 false 模式，不構成 production ownership 或 native 驗收證據。

本機驗證使用 `pnpm check`、`pnpm test:contracts`、`pnpm test:backend`、`pnpm test:fetcher` 與
`pnpm test:infra`。未提供 private FactSet native captures 的案例明確 skipped；真實 provider entitlement、
production drain／attestation／native 驗收與 rollback 均須另依部署 runbook 執行。

## 依賴安全更新

套件版本以 `pnpm-lock.yaml`、`backend/uv.lock` 與 `fetcher/uv.lock` 為準。修復間接依賴前，
先確認實際依賴鏈、上游版本範圍與 peer dependencies；避免為了解除告警而全面更新標示
`latest` 的 TanStack 直接依賴，或把所有套件強制覆寫到同一版本。

- TanStack Router／Start 與 Devtools 的 Solid 可能使用不同的 Seroval 範圍。
  Solid 1.9.14 的 `~1.5.4` 不接受 Seroval 1.6.x；目前使用 Solid 1.9.17 搭配
  Seroval／seroval-plugins 1.6.8，不以 override 繞過範圍。
- React Start 固定為 1.168.60、配套 React Router 固定為 1.170.41，讓
  start-server-core 使用 1.169.39，修復 server-function 回應的未登入反射型 XSS
  [CVE-2026-102989](https://github.com/advisories/GHSA-qx66-fv34-fjm8)。不要僅更新 Seroval
  就視為已修復此 transport 漏洞；更新框架時維持其精確配套版本並驗證正常 GET／POST RPC。
- Vitest 與 `@vitest/browser-playwright` 必須一起更新；其 browser、mocker、runner 等內部
  套件具有精確版本與 peer 限制。目前使用 4.1.12，修復 mock registration code injection
  與 Windows redirect allowlist bypass；Playwright 維持既有版本。
- Vite 最低版本為 8.1.6，修復內嵌 launch-editor、檔案 allowlist 與 custom middleware
  漏洞。Devtools 另有獨立的 launch-editor 依賴；`pnpm-workspace.yaml` 只覆寫
  `launch-editor@<2.14.2` 至 2.14.2，避免舊版仍留在依賴樹。上游已不再解析受影響版本後，
  可移除這個 override，但仍須重新執行 audit 與 Dashboard suite。
- Backend 與 Fetcher 的 certifi 均使用 2026.10.19，移除受 compromised intermediate
  keys 影響的 emSign root 信任；更新 lockfile 後必須重建部署 image，既有 image 不會自動
  換用新的 CA bundle。Backend 的 pre-commit 間接依賴 virtualenv 使用 21.14.12，
  高於 activation scripts command injection 的修補下限 21.14.7。
- Dashboard 未使用 `@tailwindcss/typography`，不安裝此套件；避免引入其精確鎖定的舊版
  selector parser。日後需要 typography 時，重新確認 parser 修補版與上游相容性。
- Backend 透過 FastAPI 引入 Starlette，更新時需驗證認證、middleware、Source payload
  限制、StaticFiles 與 Serve 查詢；不能只以 resolver 接受版本作為相容性證據。

更新後以 frozen lockfile 安裝，執行 `pnpm check`、`pnpm test`、`pnpm build:dashboard`
與 `pnpm audit`。
另驗證 TanStack serialization、登入、權限保護與 SSR／hydration；browser tests 目前可能
輸出既存 TanStack virtual-module pre-transform 訊息，必須比較基準並檢查測試退出狀態。
GitHub 告警需在修復整合至預設分支並重新掃描後確認關閉，本機通過不能代替遠端結果。

本次修補對應 GitHub Dependabot #50–#63：certifi 兩項、virtualenv 四項、launch-editor
三項與 Vite 五項。另由 npm audit 發現並修復 Vitest mocker 的
[GHSA-qxg6-89mg-rqv8](https://github.com/advisories/GHSA-qxg6-89mg-rqv8) 與
[GHSA-xp26-3454-mjgp](https://github.com/advisories/GHSA-xp26-3454-mjgp)。

Shioaji SDK 版本集中於 `providers/shioaji.py` 的 `SHIOAJI_SDK_VERSION`，與 `pyproject.toml`
的 pin 一致。Shioaji 會從 PyPI 索引移除舊版（1.7.1–1.7.6 已不在索引，只剩 lockfile 內的檔案
網址可下載），此時 Fetcher lockfile 無法重新解析，需升級 SDK。Fetcher CI 安裝 `shioaji` extra，
`test_shioaji_sdk_contract.py` 對已安裝 SDK 檢查 adapter 使用的 client 方法參數、`contracts`／
`Contracts` 綁定、`ContractsApi.get`、`KBars`／`UsageOut` 欄位與審核過的 `BaseContract`；
不建立連線、不登入。其餘 Shioaji 測試使用離線替身；login、contract lookup、kbars 與 usage
的實際行為仍須於 staging 以真實帳號驗收。

Production SSR、匿名權限導向、登入 POST RPC、已登入營運台與 Lookup hydration smoke
通過。Smoke fixture 須先呼叫 `/fixture/reset` 初始化資料；未初始化時不應期待有執行中的
Pilot。真實 provider 驗收與 runtime image 驗證仍依各自流程執行。
安全依賴驗收（2026-10-23）的 `pnpm audit` 各嚴重度皆為零；這是 npm 依賴掃描結果，
不代表 GitHub 告警已關閉。

## CI 與測試隔離

CI 長時間沒有進度時，先查 workflow run 與各 job 的 `status`、`runner_id` 和 `steps`，
並核對 [GitHub Status](https://www.githubstatus.com/) 的 Actions 事故時間。若 jobs 都是
`queued`、尚無 runner 與 steps，應先追查平台排程／狀態回報、concurrency 與 runner 可用性，
不能直接判定為 pytest、DB lock 或 build 卡住；`started_at` 單一欄位不足以證明已執行測試。
平台事故期間先保留原 run，恢復後再確認是否繼續；若已失敗，再依實際失敗 jobs 重跑。

FinDB CI 將測試分成三個 jobs：`backend` 保留 format／lint／type checks、published contracts
與非 migration tests；`backend-migrations` 保留 Alembic smoke 與全部 migration tests；
`infra-tests` 獨立執行 `infra/tests` 且不配置 PostgreSQL service；不需資料庫的部署 shell
suites（Fetcher inventory、full-market repair 與 checkpoint entry）放在 `infra/tests`。
GitHub 4 vCPU runner 上 Backend 與 Infra 固定四個
workers；migration job 維持序列執行。Migration tests 只涵蓋 rollback 下限
（`docs/operations/deployment.md`）及之後的 revisions；`test_schema_drift_migration.py`
比對 `alembic upgrade head` 與 ORM metadata 的結構、CHECK 名稱及 server defaults，
三類差異均直接使測試失敗。Partition children 僅由 migration 管理，不當成 ORM drift。

Schema reconciliation revision `1e68a0c9b3d2` 的回歸測試涵蓋前一版 populated DB 升級、
16 個 CHECK 的 OID／定義保留、缺少的市場代碼 CHECK、降版再升級與既有資料保留。
既存無效市場代碼會使升級 transaction 完整回滾；revision、原約束及資料必須維持原值。
Raw SQL INSERT 同時驗證 ORM 建表與 migration 建表的 boolean／integer／string／JSONB
預設值，並以 `Asia/Taipei` session 驗證日曆時間戳代表正確 instant，不能由 Python defaults
掩蓋 DB default 缺漏。這些測試只操作 session 自行建立的資料庫。

Fetcher deployment transaction harness 以 fake Docker／sudo／sleep 驗證十五個 consumers；
每個案例保留 90 秒程序上限，供 CI 四個 workers 下數百次模擬 CLI 啟動使用。此上限不是正式
部署 SLA；實際健康觀察次數、原 container 身分、checkpoint 與 rollback 斷言仍須全部通過。

Migration job 另以正式 `backend/Dockerfile` 建置 image，執行
`infra/tests/test_backend_runtime_container.py`。測試從 deploy helper 擷取實際 migration container
argv，在獨立 internal Docker network 與 PostgreSQL 16 上驗證空 DB、先前 accepted revision
`a42c89d3e610` 升級、exact head、重跑與 sentinel 保留。Runtime image 不掛 repo、不含 Fetcher／
pytest，不允許對外網路或寫入 root filesystem；fixture 只清理自己建立的 containers／network。
一般 Infra suite 未指定 image 時略過這兩個案例；CI migration job 明確設定 image，不能略過。
本機重現：

```bash
docker build -t findb-backend-runtime:local -f backend/Dockerfile backend
FINDB_TEST_BACKEND_IMAGE=findb-backend-runtime:local uv --directory backend run pytest ../infra/tests/test_backend_runtime_container.py -v
```

針對特定 migration 的欄位差異或 downgrade guard 測試，必須明確指定該 revision，
避免後續 additive 欄位或較新的 rollback 下限改變舊測試的範圍。升級至 `head` 的整合測試
仍驗證完整 migration chain；TAIFEX evidence 測試另驗證舊 item 保留、新欄位預設 NULL，
以及拒絕 downgrade 後 revision 與完成證據均保持不變。
Shutdown transaction 的獨立情境由 pytest parameters 分配給 workers；每個情境使用自己的
暫存目錄與 Docker state，交易內的操作／assertions 仍在同一 case 依序執行。

Fetcher release shell tests 執行完整 helper，僅將主機 account governor 與 readiness roots
映射至各 case 的暫存目錄；fake sudo 拒絕操作該目錄以外的絕對路徑，模擬必要的 ownership／
mode metadata，不建立或修改主機 `/var/lib/findb-account` 與 `/etc/findb-full-market`。
Pilot／Full profile 與 flag 開關仍執行 governor 建目錄、bind mount、preflight 與 rollback
分支，並驗證建立的 container arguments；測試不需要 root、真實 Docker release 或 provider 連線。

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

Backend／Infra 的跨 application 回歸測試以 `sys.executable -I` 執行實際 Fetcher CLI 與 reader，
驗證部署 checkpoint 邊界。Fetcher 是 Backend 的 editable dev dependency，isolated mode 下
`findb_fetcher` 可匯入、Backend `app` 不在 `sys.path`；因此只需 Backend frozen dependencies，
不需另建 `fetcher/.venv`、provider extras、AWS 或 provider credentials。Fetcher 依自身 lockfile
的完整驗證仍由 Fetcher CI 負責。

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
使用一般 session，結束後依 FK 反向順序 DELETE 所有 ORM tables 並重設 sequences；這些表
每個測試只有少量資料，逐表 `TRUNCATE` 每次約 130 ms，DELETE 約 10 ms。這些清理只發生於該
session 的暫存 DB。
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
同時驗證一般 session rollback、直接 engine 測試後的清空、基底 schema／資料不變，
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


以下舊版五 feeds native evidence probes 仍保留供問題追查；尚未完成七 feeds 與有效日曆政策的 native 驗收，
不能作為新版部署 gate 或宣稱新版資料驗收通過。新版 fixture 與部署驗收見下方兩節。

Staging provider functional acceptance 由 `fetcher/tests/test_staging_pilots.py`、
`backend/tests/test_staging_provider_pilots.py` 與 staging evidence probes 覆蓋。CI 必須核對 catalog 與
runtime/schema/Source scope，缺少 future provider pilot／startup acceptance 時 fail closed。
此 legacy diagnostic catalog 的 runtime metadata 必須綁定 `findb-fetch-runtime`、明確
`--execution-profile pilot --provider ... --config configs/full_market.staging.v1.json` 與 config hash。
Validator 與 evidence collector 共用相同驗證，拒絕已退役 CLI、錯誤 profile/provider/config 或 hash。
`--check` subprocess 僅證明入口與設定可用；保留的舊固定 cohort 仍只供診斷，不代表新 runtime
目前的 frozen members、七 feeds native 驗收或部署 gate 已通過。

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


## 環境與 profile 一致性

兩個 AWS 環境共用部署 schema、設定 loader、必要 gates 與健康驗收。
`test_execution_profiles.py` 覆蓋七條 feed 的凍結樣本與 profile 計畫隔離、canonical 覆寫後持久化證據及缺漏告警；
`test_execution_profile_migration.py` 驗證升級保留與 rollback 下限。
`infra/tests/test_full_market_checkpoint_entry.py` 執行 workflow 實際傳輸的 checkpoint guard；
Fetcher host／shutdown／inventory suites 覆蓋停止、訊號、遺失狀態及失敗回退。
Shutdown 與 inventory suites 的假 `docker`／`sudo` 由 `infra/tests/fake_daemon.py` 提供：每個指令是只
轉送參數與指定環境變數的 bash client，狀態與判斷集中在 pytest 行程內的 localhost daemon 並逐次序列化。
Shutdown 的 activate 案例約上千次呼叫，不再各自啟動 Python 直譯器。
執行部署／shell 腳本的測試以 `script_env()`（`backend/tests/script_env.py`、`infra/tests/script_env.py`）
建立環境：只帶入 `PATH`、`HOME`、`TMPDIR` 與 locale，其餘變數由測試明確設定，避免開發機或 CI 的
`AWS_*`、`APP_ENVIRONMENT`、`FETCHER_*`、`GITHUB_*`、`ACTIONS_*` 改變腳本行為或寫入 `GITHUB_OUTPUT`。
執行 repo 自身 Python／pytest 子行程的測試（例如 migration、ORM 隔離）仍繼承完整環境；
`backend/tests/test_script_env.py` 掃描 backend／infra 測試：每個 `subprocess` 呼叫都須明確傳入
`env=`（省略即隱式繼承），並阻擋其他繼承完整環境的寫法。
AWS 實際部署與七條 native feeds 驗收是另外的結果，不以 fixtures 或靜態檢查替代。

2026-10-15 staging Pilot 已核對四條 feeds：FinLab `tw_equity_eod`／`tw_etf_eod`，
以及 Shioaji `tw_equity_minute`／`tw_etf_minute`。每條計畫均為
`expected=2, data=2, no_data=0, missing=0, blocked=0`，Source receipt、normalization、
canonical 與 Serve projection 一致，R2 bytes／hash 與 frozen checkpoint 亦符合；
minute 同時具完整 sequenced snapshot。
Backend 最新唯讀證據命令為 `f7b10581-110e-4241-a3ad-6d5559c6cd6e`，
FinLab Fetcher 為 `aad9978e-a510-4ea7-b702-accfbaecae04`，
Shioaji Fetcher 為 `af29aac1-217c-421f-8d4e-37932b93adca`。
七條 feed assembler 判定這四條通過，整體仍為 4／7。
Owner 於台北時間 2026-10-16 將香港移出本次 native 驗收；本次目標改為其餘六條，
美股後續驗收通過後目前為 5／6（證據見下文），待 TAIFEX 通過後才接續 production。這是驗收範圍調整，
不代表香港成功；七條 feed 的部署／fixture 範圍、香港既有計畫與失敗紀錄維持。
追加單次香港 HKEX 診斷 `c91545a9-1d55-4240-acd7-f524b138b477` 仍回傳 HTTP／provider code 400。
診斷後由 Owner 停止 Twelve Data Pilot，`d5e9e9c1-844f-4d7f-9b4e-55842d976913` 確認
Pilot／Full market／account checkpoint integrity 正常，active leases、prepared 與 armed 均為 0。
[Twelve Data 官方說明](https://support.twelvedata.com/en/articles/12682324-end-of-day-eod-pricing-market-data)
將 XHKG 列為需授權或未支援市場；實際帳號可用性仍待確認。
FinLab 資料完成後主機仍於 11:35 UTC 發生 global OOM，所有 Pilot 已停止；
資料證據通過不代表 runtime 穩定性、Full market 或 production 通過。
剩餘 provider 問題見 [原生驗收待辦](../dev/production-foundation-cutover-plan.md)。

FinLab FileStorage 與各 feed 目錄缺欄隔離修復已經 PR #304／#306 部署；
該輪部署後 staging Fetcher 為 `50cb51faadd9`，Fetcher CD `38769679416` 成功。
部署前 RDS snapshot `findb-staging-native-20261015-1605` 為 available，
Fetcher 備份為 `/var/backups/findb-unification/staging-20261015T163004Z`。
Owner 允許的 historical Shioaji 短暫啟停命令 `135b700c-be9b-42fa-86d7-af58cd092f5c`
正常退出（exit 0、未 OOM、image 不變），未建立歷史工作。
部署後 `97771109-be24-44ae-8642-0dea78395605` 確認 11 個容器使用 accepted images，
並逐一比對備份所列全部 16 份 SQLite 的 durable rows、配額及 cooldown。
`e2ba2c33-7b4e-4315-b16d-9d162eda0cd5` 確認 Backend 八組資料指紋、首次日期與 controls
完全一致，所有抓取控制仍為 stopped。

三輪普通、受 governor 控制的 FinLab universe refresh 診斷
`c8f1fedb-fc6c-4ec4-adc8-30f647a4f5ed` 在同一程序執行，三輪均為 exit 0，
實際 storage 為 FileStorage、`use_cache=false`、`cached_frames=0`。
RSS 依序為 273,304／284,036／284,036 KiB；peak RSS 為 284,024／284,960／290,900 KiB。
股票 1,948 項與 ETF 361 項目錄依既有規則發布，第二、三輪重用相同 release；
這不表示每個商品都有資料。唯讀查詢 `4ee06208-f870-4b14-83cb-91eefac42d6d` 與
`cfdbf8ee-f1cb-45f3-b722-a992df8669a8` 確認 equity mapped 1,947、gap `8908`，
ETF mapped 360、gap `00415A`；不再因單一 ETF 缺欄將整批 equity 標成 gap。
相同查詢也確認三輪診斷未改寫既有 plans、首次日期、歷史工作或 controls。
健康命令 `70d6fad4-920b-413c-a6a2-ed93bbdfa499` 未見此次部署後新 OOM，
11 個容器 restart 均為 0。
這是目錄多輪穩定性證據，沒有新 market delivery，不增加 4／6 的 native 通過數；
仍需多輪日常抓取及同時運作的記憶體觀測。Production 尚未進行本輪 native 驗收。

TAIFEX 同日期目錄修正 PR #307 已合併為 `0a8d491a064d`，完整 `pnpm check`／`pnpm test`
及修正後完整 Fetcher 1,394 項測試通過。獨立 `gpt-6.1-sol` High 最終審查為 CLEAR，
另執行 Fetcher 118 + 355、Backend 66 項針對性測試與修改模組 typecheck。
部署前備份命令 `8a9d2d59-52a9-4aaa-afec-4c51f411652f` 確認 11 個 runtime 全部優雅退出，
16 份 SQLite 保存於 `/var/backups/findb-unification/staging-20261015T170222Z`，
archive SHA-256 為 `7db30f43f177e1229885a9d8d705701061755e6d22e7b8fe59a665f62821c059`；
RDS snapshot `findb-staging-native-20261015-1702` 為 available。
Backend CD `38775214962` 成功；`5eae0a88-778c-499f-bc22-3afce819015b` 核對實際安裝版本，
八組資料指紋、controls、首次日期與既有 plans／歷史工作均未改變。
Fetcher CD `38775214709` 已成功。台北 2026-10-16 07:02 的命令
`6e170400-ef45-45aa-b931-9218398752b7` 確認全部 16 份 SQLite integrity 正常，
既有 durable rows 與備份相同、quota／cooldown 未倒退，11 個容器均使用 accepted digests。
`8b9ef1b3-36f6-4691-8d16-c698268f63a3` 再次核對 Backend 八組資料指紋與部署前相同，
所有 controls 均 stopped。`58f2fb93-8e5f-428a-a0f5-ad2149e13116` 確認 Fetcher
自本次部署後沒有重啟或 OOM，可用記憶體 761 MiB。
Backend 健康命令 `8789d81c-6a7b-4b26-a969-d44bde78d4f8` 確認 ingest／serve HTTP 200、
服務健康與 queue check 通過，DLQ／過期 lease／未發布 outbox 均為 0；
既有 missing deliveries 29 與 retry exhausted 1 仍保留，不因部署驗收通過而結案。
本次不重設 2026-10-15 的 TAIFEX terminal checkpoints，也不以部署通過代替 native 驗收。

`fetcher/tests/test_finlab_file_cache.py` 覆蓋五欄位重複呼叫的 frame 釋放、
實際 storage 與舊 package alias、storage replacement、各錯誤路徑及 SDK lock。
驗證 optional FinLab SDK 時，另以固定 1.5.7、封鎖外網的測試檢查真實 FileStorage：
有效磁碟檔案重複讀取不下載、bytes／expiry／created 不變，以及 CacheStorage 不被清除。
外部 process 更新 pickle 後，`use_cache=False` 沿用 SDK 原生 `_created` 行為；
`.recent` 可能使用較舊的 `time_saved`，須驗證 hash 不符仍走原生完整下載 fallback。
不把所有過期／外部改寫檔案的請求完全相同列為保證，也不以離線測試取代 staging 記憶體觀測。

## 有效日曆政策回歸

`test_effective_calendar.py` 覆蓋 staging／production、pilot／full_market、七 feeds 的無日曆／週末／跳過政策，
以及 draft、部分發布、閏年與不寫入的預設查詢。`test_effective_calendar_migration.py` 與 profile migration 測試
驗證歷史 item／plan／首次日期保留，標記 migration 時點快照。Fetcher recovery 測試驗證週末仍取得 Source 決策，
skipped_calendar 不建立本機抓取工作。Dashboard 驗證來源標示、政策略過與 profile cache 隔離。
`pnpm check`、`pnpm test` 與兩環境 IaC format／validate／plan 必須通過後才能部署。
以上架構與 fixture 驗收不代表 provider／R2 native 資料問題已排除。

2026-10-16 US 排程到期後，凍結 Pilot 計畫 `01a141cf-70f7-7ad5-a2f9-ecfa9002bd9e`
選定 `A`／`AA`，兩者尚未交付。單日查詢診斷
`cbf0694e-d81d-4317-9470-59a0b2eb9919` 回傳 HTTP 400，最新兩筆診斷
`f63c484d-c78d-4786-b506-c9f8120f0d35` 則含 10 月 15 日。
同一商品改以 10 月 15 日至 16 日為 provider 範圍的診斷
`66fe44c0-b9d5-4b2b-9c52-d88e7eca9d97` 成功且只回傳 10 月 15 日；
此證據支持修正同日查詢的上界，不代表完整 native 交付通過。
診斷後 Owner 已停止 Pilot（revision 23），`0963c33a-1323-4cc7-b911-54784e5bd6d0`
確認 Pilot／Full market／account integrity 正常，active leases、prepared、armed 全為 0；
US 兩項工作仍 pending，各保留四次嘗試，香港失敗紀錄維持。

本次日期修正部署前，備份命令 `c023be63-db49-45d9-bb9a-f164e8c38382` 已確認
11 個 Fetcher 容器優雅退出，16 份 SQLite 存入
`/var/backups/findb-unification/staging-20261015T232242Z`；archive SHA-256 為
`c2fc293951db5804cbd1c66861fd0b96caf8a83ebc5bbdef589eec1052e20e90`。
Backend 部署前唯讀基準 `2d4d896c-aceb-47f7-81f7-d5c36526d6dc` 保存八組資料指紋，
包含新增 US 計畫，所有 controls 仍 stopped。

US 日期邊界修正 PR #309 已合併為 `efbca9f2e3df`，Required CI `38824002467` 通過。
完整 `pnpm check`／`pnpm test` 通過：Backend 2,201、migration 54、Infra 148 加 268 個子測試、
Fetcher 1,406、Dashboard unit 192／browser 10／E2E 15。
獨立 `gpt-6.1-sol` High 最終審查為 CLEAR，另執行 227 項相關測試與四個修改模組 mypy；
審查前後程式與文件指紋一致。本次無 IaC 或環境變數變更。
Fetcher CD `38824347083` 成功，staging 實際安裝版本為 `efbca9f2e3df`。
部署後 `3171a2cf-3b96-4815-b8d3-47fee3b72b30` 確認 11 個容器均使用 accepted digests，
16 份 SQLite 與 `staging-20261015T232242Z` 備份逐表比對通過，quota／cooldown 未倒退。
Backend 未重新部署；`d3d466c8-b8d0-4446-aa63-d89e403bbccc` 確認八組資料指紋、首次日期、
計畫與 controls 與部署前完全相同。`c8a12685-3f00-41bb-b4e6-99ec8f7698dd` 確認
服務健康、ingest／serve HTTP 200 與 queue check 通過；既有 29 項 missing deliveries、
1 項 retry exhausted 仍保留，DLQ／過期 lease／未發布 outbox 均為 0。

修正部署後 Owner 短暫啟動 Twelve Data Pilot（revision 24），Dashboard 於台北 07:53
顯示 provider fetched 與 normalization completed，兩條 feed 中一條完成。
隨後 Owner 停止至 revision 25；`c5b64289-8b00-4067-b122-a50cba48bef4` 的純統計核對顯示
Twelve Data 兩項 complete、兩項既有 manual，所有 profiles 的 active leases、prepared 與 armed
均為 0。完整 native 驗收另外依下列 Backend／R2 證據判定，不以工作統計取代。

Owner 明確授權將 staging native 證據經 SSM 傳回本機 `/private/tmp` 後，
台北 2026-10-16 08:41 完成唯讀匯出與七條 feed assembler 核對；未啟動任何抓取。
AWS MCP 當時回報工具不存在，改以 staging AWS SDK 呼叫既有 SSM transport／exporters；
先核對帳號 `439622209937`，本機證據檔權限為 `0600`，不包含 credentials。
Backend 命令為 `4a8cc257-9104-4999-aa06-df4231c8c276`，
Twelve Data Fetcher 為 `2cf10532-9e3e-4cd4-8a83-fc53c36fdace`。
美股 `us_equity_eod` 的原凍結計畫 `01a141cf-70f7-7ad5-a2f9-ecfa9002bd9e`
為 `expected=2, data=2, no_data=0, missing=0, blocked=0`；兩項 checkpoint 工作均 complete。
抽樣商品 A 的 run `01a141fb-fe7c-706e-bd7d-6705553f614d` 與 normalization job 均 completed，
Source receipt、frozen plan／checkpoint 識別及 request hash 一致；R2 實讀 296 bytes 並驗證 hash，
canonical projection 與 HTTP 200 Serve 回應完全一致。此證據為整個計畫完成加每條 feed 抽樣鏈路核對，
不宣稱逐筆驗證全市場資料。
同一輪再次核對 FinLab／Shioaji 四條通過；原七條 assembler 為 5／7、整體 `passed=false`，
香港已排除本次目標，因此本次 native 範圍為 5／6。
TAIFEX 舊計畫仍為 `native_plan_incomplete`，待台北 2026-10-16 17:00 新日期計畫；
香港仍為 `nonzero_native_sample_missing`，不因排除而標成成功。Production 驗收尚未開始。

## Provider work 整合測試

Backend dev group 以 editable path 安裝 Fetcher，僅供真實跨 application 測試；production `uv sync --no-dev`
不會安裝 Fetcher 或 provider SDK。`test_provider_work_runtime.py` 以 sync transport bridge 將實際 Fetcher
FullMarketRuntime／historical runner 連至 authenticated ASGI Source，再執行真實 normalization、DB fencing、
canonical proof 與 shared flock；只替換外部 provider response／R2 upload。它驗證 Full → historical → Pilot
handoff 與 controls 保持 running。`test_provider_work.py` 另涵蓋 queue enforcement、四 provider historical
授權、acceptance／close transaction barrier、FIFO／retry／heartbeat recovery 與 retained-raw rerun。

`test_provider_work_adapters.py` 的 enabled native runtime 矩陣完整執行五個 historical scopes：
Twelve Data US equity EOD、FinLab TW equity EOD、Shioaji TW equity minute／TW ETF minute，以及
TAIFEX TW futures EOD；使用真 adapter、Source client、authenticated ASGI、worker 與 canonical proof，
僅以 fixtures 取代 native SDK／HTTP 回應與 R2。Shioaji fixture 仍執行實際隔離 child 的 IPC／FD protocol，
不需下載 provider SDK 或持有 live credentials。Harness 將 Shioaji 5/s 的帳號 pacing 間隔縮為 10 ms；
pacing floor 與等待路徑仍執行，實際間隔由 Fetcher 單元測試覆蓋。

Production Shioaji 另以真實 reviewed v2 manifest 驗證股票 50／ETF 3 條 sequences 經
FIFO → authenticated Source → worker → canonical，逐一對帳 receipts、member evidence 與
sequence_count；既有 recovery cases 明確使用四成員 pilot fixture。Backend historical tests
涵蓋 production scopes、preview／create／idempotency／cancel、credential scope／撤銷／lease、
台北日期跨 UTC 邊界與 Shioaji 最近 31 天上下界，以及凍結日曆收盤授權。
Fetcher tests 驗證 production manifest／dataset 拒絕、dry-run／deliver 的 50／3 calls 與未知 rows、
當日／30 天前的完整 raw-first 交付及 checkpoint 重開不新增 SDK calls；超過 31 天與未來日期不抓取。
Production 50 成員 nominal 驗收使用 `shioaji_dispatch_clock`，讓 parent、forked fake SDK child
與 Source client 共用 monotonic clock domain，避免平行 suite 的程序排程延遲耗盡每成員的
50ms dispatch budget；durable quota／window wait 仍依 FIFO 重試並由局部 account clock 推進。
這只改變測試時鐘座標；產品 grant／quota／lease 下限、IPC timeout 與 process termination
不放寬。過期、FD／owner fencing、真實授權失效及跨分鐘案例由獨立 dispatch boundary tests 覆蓋。
50 成員的 harness 設定 `execution_timeout=90`；`RuntimeHarness.run()` 會依此重設 deadline，
不能只改 `deadline` 而沿用四成員 pilot 的 25 秒預設，否則平行執行可能在中途造成 heartbeat
與 settlement 被測試本身截斷。

Fault cases 包含 intent commit ACK 遺失、local save 後
confirm 前取消、local save crash、intent／cancel transaction barrier，以及 daily stop 後 prepared 排空。
取消／停止期間不新增 provider I/O，accepted nonterminal run 仍保留 whole-work owner；取消狀態與缺漏保持真實。
`backend/tests/test_provider_work_projection.py` 以 PostgreSQL 驗證大量 retained rounds 與約 42 MB
minute prepared 仍只執行固定三次 SELECT（含 historical／HK eligibility 最多五次），不讀取完整
body／manifest／scope／proof／token，不因工作數增加 N+1。涵蓋五 providers、Pilot／Full、
terminal／nonterminal receipts、原 prepared 排空、pending error report、window／cooldown／
not-before／batch-yield／recovery、historical expiry eligibility 與 current／historical failure 顯示，
並透過 authenticated Viewer API 驗證原回應與 401／403。效能數據只作量測，不使用易受 CI 負載
影響的毫秒斷言；SQL 形狀與呼叫數是回歸條件。Production endpoint 上線驗收仍須另行執行。

Admin single／bulk endpoint 測試驗證新 run／audit／FIFO fence；failed rerun 使用實際 worker failure path，
不以直接改 run status 代替 normalization。Migration case 保留舊 prepared body／raw hash／receipt，
停止後只允許同 work 原 payload 導入 descriptor 並 replay。

Closed-round 回歸涵蓋 daily、FinLab、Twelve Data 與 Shioaji：close commit 後失去 ACK，復原不新增
provider I/O；partial frozen scope 先安全 retry，再完成原 members。Shioaji 多日期／三 ETF sequences 的
429、503、lost accepted ACK 與 terminal polling timeout，分別在 running／cancelled 狀態下驗證原 bytes、
tail FIFO、後續日期不被取消、budgets／receipts 保留，以及 cancelled 只排空原 prepared。
Daily 另驗證 stopped 429／503 retry 可 claim，及同日較新 Pilot 寫入後的 explicit manual failure；
該 overlap 的 actual-write evidence 不偽造，且結束後不再阻塞 provider。
Stop／drain handoff 另驗證原 prepared 已 terminal、只剩未抓取 members 時，stopped queued work
不能以「曾有 prepared」反覆占據 runnable head；maintenance consumer 可直接前進。合法 start 同
profile 後由新 attempt 接續同一 frozen scope，保留 first date、原 bytes／receipts 與 failure budgets。

Fetcher `test_provider_work.py` 使用真 subprocess 驗證 orphan FD 持鎖、timeout terminate／kill／reap、
uncertain-stop quarantine 與四種 native response 的 raw-upload crash／restart。Migration 與 host preflight
測試分別位於 Backend `test_provider_work_migration.py` 與 Infra `test_provider_work_preflight.py`。

同一 native scope 矩陣另驗證 Source 408／503 tail retry **入列後**才取消的排空，以及 DatasetInactive
永久拒絕：取消不新增 native I/O，原 prepared 可繼續；永久拒絕保留實際 status／code／digest 與
manual pending identities，安全結束後 provider 可用。Source／Admin single／bulk 三路 retained-raw
rerun 使用真 worker 驗證較新 canonical precedence 的零寫入 failed settlement 與 lost-ACK replay。
Scoped FIFO 測試以兩把不同 dataset 權限的 authenticated Source keys 和 transaction barrier，驗證
較窄 consumer 不能超越全域 provider head、其他 provider 可同時前進。完整 Alembic upgrade 測試
覆蓋 stock／ETF minute 原三個 sequences 中已完成前兩個的 partial group，保留原 checkpoint bytes、
raw／receipt IDs，再 replay 前兩個並交付第三個至真 normalization／proof settlement。

`test_provider_work_daily_faults.py` 以 actual daily adapters、authenticated Source、真 worker 與 proof
覆蓋現有七條 feeds（含僅支援 daily 的 HK EOD／TW ETF EOD）；429、503、lost accepted ACK 與
native network fault 後同一 attempt 不再新增 member acquisition／intent，下一 attempt 完成原 scope。
`test_provider_work_transitions.py` 覆蓋 failure-induced sibling cancellation 後無 historical consumer 的
maintenance claim，以及 actual account governor 四次 predispatch quota wait 後恢復；保留真正 dispatch
failure 的原 budget 與 account usage。SQLite frozen-v9 DDL 測試驗證 transactional v10 upgrade、升級
中 hard crash／reopen、原 rows／prepared／receipts／pins 不變，未知或實際失敗不退款。

FactSet runtime harness 的 native snapshot `fetched_at` 與 Source fixture 共用同一個 UTC-aware
測試時鐘；historical、daily 與 prepared drain 都必須在 harness 安裝，避免固定服務時間與真實
producer 時間漂移。偏移一天的 pipeline regression 驗證 raw timestamps 與服務時間一致；不得
放寬 production 五分鐘 freshness 限制，也不移動 account pacing、monotonic deadlines 或 lease 時限。

共用 `RuntimeHarness` 的所有 TAIFEX cases 透過測試限定的共用 monotonic clock 控制 acquisition 與
Source grant receipt，涵蓋 historical、daily、maintenance 與 prepared retry；不能只在單一 test file
安裝時鐘。UTC、account pacing、asyncio 及 harness timeout 預設仍使用真實時鐘。這些案例驗證狀態轉移，
不應要求繁忙 CI runner 在 50ms 內完成真實 DB／ASGI 往返。另以同一時鐘在最後 work
authorization 返回時推進 51ms，驗證原 50ms grant 失效、無 native I/O／actual_start、queued retry，
再以新 grant 完成同一 work。Production 時限及 dedicated Fetcher elapsed-time tests 均保持不變。
`account_clock` 推進 governor wall clock 時保留已安裝的 dispatch monotonic clock；helper regression
驗證組合後仍會拒絕過期 grant，且 teardown 還原兩個模組的時鐘。Prepared retry cancellation 的
408／503 案例另注入實際 60ms Source work-authorization 延遲，確認原 prepared bytes 可繼續排空、
不新增 native I/O。

Provider-work native harness 以 fork 注入離線 Shioaji fake；production `_ProviderProcess` 使用
`subprocess.Popen`／exec 與明確 `pass_fds`。測試在再次啟動 native child 前，所有 quota 注入與
SQLite diagnostics 都必須先 commit、再 close；`sqlite3.Connection` 的 context exit 只處理 transaction，
不會 close。不得將 live governor SQLite／WAL VFS state 帶入 fork child，否則 cleanup 可出現
`SQLITE_IOERR_SHORT_READ`。Fake launcher 的 `sdk-boundary-errors.jsonl` 只記 SQLite error name／
frame location，不保存 SDK exception message、payload 或 credentials；SDK timeout／terminate／kill／
reap 邊界維持原設定。完成 normalization 的測試 stub 必須提供與該 member records 一致的
`total_records`／`success_records`，不能只給 status 而把 AttributeError 當作 Source error。

`test_provider_work_maintenance.py` 以 authenticated ASGI／owned PostgreSQL 驗證 explicit／automatic
兩條 maintenance 路徑在 daily controls stopped 時，四 provider 的 native catalogue／official HTTP 503
都能安全結束 round、tail retry、釋放 host FD 並直接 claim 下一工作；另有 transport、403、格式拒絕、
真正 predispatch quota 四次等待及跨 runtime restart 原預算耗盡驗證。
Daily native 十二資料 HTTP 429／200-code429 測試只涵蓋已支援的 US／HK daily feeds，historical 只用
US scope；以 native MockTransport、真 Source／normalization／proof，分別驗證原 daily 8 次與 historical
schedule 上限、cooldown 跨 tail／重啟保存、耗盡 manual 或最後合法 attempt 成功，沒有增加 historical scopes。
Account clock fixture 只移動 governor、PG queue／retry sender 與已由 Source 驗證的回傳 authorization 時間座標，不清除 usage、
cooldown、audits 或原 failure counter。

Native HTTP429 使用真 `Retry-After: 120`，驗證原 tail `not_before` 超過60秒、journal cooldown 跨重啟
保存，且同 provider 的 authenticated retained-raw rerun 在原 work 冷卻時可取得 fence、完成真 worker
normalization／settlement；原 native call count／account ledger 不變，原工作到期前仍不可 claim。
`test_observed_cooldown_survives_restart_fences_new_io_and_sets_tail_deadline` 另驗證 journal reopen、
fence 優先、pure predispatch wait、原 prepared bytes 與 tail deadline，heartbeat failure 不會降級成等待。

Maintenance 503 的有效body與被bounded reader拒絕的body都保留實際HTTP status並安全tail retry；
原public ReadinessBlockedError／quota exception相容性仍驗證，malformed HTTP200維持明確failed/manual。

Maintenance 實際 Source／owned PostgreSQL matrix 另納入四 provider 的 transport／403／malformed200／
predispatch quota siblings，以及 TAIFEX 官方 HTTP200 `{}`、空list、非object row、缺Date、Date非字串、
無效／不一致日期、有效日期但錯誤report body；兩入口都須明確 failed/manual、owner free、下一runnable
可claim，且不能產生 no_data proof。FinLab／Shioaji malformed200 依現有pipeline先取得4份official
snapshots才解析，TAIFEX官方JSON格式案例僅1個native call；historical五report/manifest驗證維持原路徑。

`test_provider_work_first_plan.py` 不預建 TAIFEX daily plan：actual native latest503／dated503、純quota、
not-published 與永久403均驗證安全round／下一工作claim；依原Admin規則發布dated universe後，同一
work完成，valid native reports不重抓。`test_provider_work_not_published.py` 與 daily fault suite驗證
TD US historical／US+HK daily 在 fresh runner 下從舊日期200回覆恢復，首原operation真正native兩次，
negative bytes/hash、原budgets及全scope保留；不增加HK historical。Fetcher journal tests另驗證valid／
invalid legacy cache升級、prepared與budget不變，以及有效raw cache的原crash/restart行為。

`test_provider_work_dispatch_fence.py` 以 HTTP MockTransport、FinLab SDK gateway 與 production Shioaji
child protocol 驗證 governor wait／account-auth return 後的 heartbeat／generation races：native calls 與
charged requests 必須為0，保持 recovery blocked；另驗 legacy governor reopen、reason/deadline單調保留及
cooldown publication ACK失聯隔離。正常SDK bootstrap與account security suites仍須通過。
`test_provider_work_unknown_cooldown.py` 以 authenticated Source／owned PostgreSQL 保存 crash-point
owner、manifest、原job budget與armed／unknown／legacy floor snapshot，然後fresh coordinator恢復；
六參數含publication ACK遺失，驗PG cooldown、原unknown liability／budget保留、prepared／retained-raw
排空、另一provider同時持有slot+flock、到期原scope完成，無claim後持owner睡冷卻。

### FinLab SDK 與過期 cutover 邊界

- `fetcher/tests/test_finlab_work_boundaries.py` 使用真實 exec'd provider worker／IPC，涵蓋
  catalogue 與 data 唯一授權點的 stop、heartbeat／generation 失效、slow fence、slow grant、
  expired IPC grant、worker 不連 PyPI 與 stop-reap；另驗證 negative table snapshot 跨 reopen 保留，
  valid cache 不新增 SDK I/O，prepared bytes／budget 不變且不序列化任意 SDK 物件。
- `backend/tests/test_provider_work_expired_cutover.py` 以 authenticated Source／fixture-owned
  PostgreSQL 跑五種 historical scopes，涵蓋 prepared expired 首項、空 expired head 加 prepared
  sibling、重複兩次 cutover、原 body 排空及後續 claim；一般 manual failure 不復活。
- `backend/tests/test_provider_work_not_published.py` 的 FinLab 案例驗證缺目標日期先保存
  negative observation，下一輪完成原 frozen members 且不清除原 budget／觀測。

這些測試使用離線 SDK／HTTP／R2 seam，沒有驗證 live provider 或 AWS rollout。

- `backend/tests/test_provider_work_live_expiry.py` 驗證五種 historical scope 的精確當地
  到期邊界、兩條授權路徑、成功授權不更新 item lease、新 manifest／intent 拒絕，及 active owner
  已保存 prepared 的原 body 排空與 `BACKFILL_EXPIRED` 保留。
- `fetcher/tests/test_provider_work_suspension_budget.py` 涵蓋 daily／historical／maintenance
  的 predispatch wait、先前 field 已完成後停止／quota wait 與原 retry ceiling；retry ceiling
  每輪使用不同 dataset，避免 durable response replay 讓該輪沒有實際派送。
  Dispatch-fence lost-ACK 測試在斷言 quarantine 後只釋放該測試自行建立且無 native child 的 handles，
  可與 FinLab tests 換序執行；產品的 quarantine／recovery 行為不變。

- `fetcher/tests/test_provider_process_reap.py` 注入 SIGTERM／SIGKILL 的 PermissionError，
  驗證已退出 child 仍完成 reap／group proof，活著或無法 probe 的 group 則 recovery blocked。
  Worker 測試只在 parent 使用假時鐘；`tests/provider_worker_support.py` 的 `child_deadline` 把
  有效／過期判定映射為 worker 的真實 monotonic 期限，保留產品 50ms grant 且不受 IPC 排程延遲影響。
  同一 support 產生離線假 `finlab`／`pymysql` 套件，只經 `PYTHONPATH` 提供給 worker launch，
  正式程式碼不加測試 hook。`fetcher/tests/test_provider_worker.py` 驗證未知操作 fail closed、
  worker 只繼承 pipe 與 whole-work FD、credential 不在 argv／environment、多執行緒 parent
  不觸發 fork 警告，以及 parent 被 kill 後 worker 仍持有 lock 至結束。

- `backend/tests/test_provider_work_daily_manifest.py` 透過 authenticated Source API 驗證
  Pilot／Full 在 claim 後停止時拒絕首次 manifest，已凍結相同內容仍可重播與安全 close／settle。
