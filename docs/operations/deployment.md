# 部署、驗收與回退

本文描述統一版本的部署契約。兩個 AWS 環境的首次切換與 native 資料驗收進度另見
[進行中計畫](../dev/environment-runtime-unification.md)；程式碼、測試或部署健康通過不代表 native provider 問題已解決。

## 發布與設定

GitHub Actions 負責編排，不使用 GitHub Environment 設定、secrets、人工審批或身分綁定。
所有部署入口從 protected `main` 執行，再解析 immutable release。OIDC subject 是
`repo:FPI-TW/findb:ref:refs/heads/main`；套用 IAM 前須讀取 repository 實際 subject customization，
不得只依預設格式推測。不同 account、unit、publisher、deploy、promotion reader 維持分離權限。
Job 先 checkout immutable release，再於 `.deployment-driver` checkout protected-main driver 並載入 AWS 設定。
載入本機 composite action 後不得重新初始化根 checkout，避免 action 在 post cleanup 前被刪除。
第三方 Actions 固定完整 commit SHA；concurrency 依環境及 unit 分離，不取消進行中的部署。

| Unit | Staging | Production |
| --- | --- | --- |
| Backend＋Dashboard | `findb-cd.yml`，main 自動部署 | `findb-production-cd.yml`，人工 promotion |
| Fetcher 三個 images、四個 providers | `fetcher-cd.yml`，main 自動部署 | `fetcher-production-cd.yml`，人工 promotion |

Staging build／reuse immutable commit images；production 複製 staging 已 accepted 的同一組 digests，不重新建置。
Production 的 annotated immutable tag 必須指向 main history 中的相同 commit，tag object、commit、bundle、
image digests 與 acceptance 全部一致。Backend 與 Fetcher 分別發布，跨 unit 變更採 Backend-first。
同 SHA 自動部署時，Fetcher 先等待 FinDB CD 結果；manual promotion 也必須遵循此順序。

Repo 僅保存 bootstrap 定位資訊。部署控制從 AWS SSM 讀取，應用設定從 Secrets Manager 的
`runtime/configuration` 讀取；credentials 維持獨立 secret。每個 consumer 的必填、選填與預設值在
staging／production 完全一致，只有 values 不同。設定驗證／同步見 [infra/env](../../infra/env/README.md)。
Runner 不讀取應用 secrets，由 instance role 在 host 以 tmpfs 載入最小 consumer scope。
`APP_ENVIRONMENT=local|staging|production` 是應用環境輸入；immutable JSON 的 `deployment_target` 必須吻合。

## 共用部署順序

1. 解析指定版本與 AWS 設定，驗證 schema、account、instance tags／instance profile、SSM Online、
   registry、release manifest 與 exact image digests。RDS 需 available、備份保留至少一天，且可還原時間不超過一小時。
2. 以實際 image 執行 configuration、DB migration compatibility、credential reconciliation 的唯讀 preflight。
   SQLite checkpoint 必須存在且完整；已安裝 runtime 的 checkpoint 遺失不得被當成新主機初始化。
3. Backend 停止公開 nginx、舊讀寫服務，優雅停止 writers；以 migration role 升級、核對 exact revision、
   校準 DB grants、deployment credentials 與 registry。兩環境執行相同步驟；不執行 calendar seed 或 completeness gate。
   Migration `91a4c7e2d630` 凍結既有工作使用的日曆與政策快照，完整保留既有 plans、first dates、prepared deliveries。
4. Backend candidate 只啟動內部 services，驗證 API、Dashboard、RabbitMQ durable topology、worker ping／heartbeat
   及 queue health；公開入口保持停止。成功後停止 candidate services，再回報可保存 acceptance。
   Fetcher candidate 執行 offline config、controls stopped 與 checkpoint 驗證，驗證最終 container config，
   不啟動未 accepted 的抓取程序。
5. Orchestrator 保存 immutable accepted bundle／record，綁定 commit、unit、target、digests、SSM command 與成功結果。
   SSM timeout、結果不明或缺少成功 marker 均不得視為 acceptance。
6. Activate 再次驗證相同 identity 與必要 gates，啟動 accepted services、公開 nginx，完成 TLS／routing／queue health，
   才更新 current pointer。Fetcher daemon 可保持 idle，抓取 desired state 仍由 Owner 人工控制。

日常 deployment 不執行 provider native 抓取 gate。不可因驗收而自動 start controls、重設首次日期，
也不可將 FinLab／Shioaji／Twelve Data／TAIFEX 或 R2 未完成的 native 驗收描述成部署成功的一部分。

## Runtime 與持久化狀態

每次 Fetcher 部署固定安裝八個 recurring runtimes：四個 `findb-pilot-*` 與四個 `findb-full-market-*`。
兩者使用同一 `findb-fetch-runtime --execution-profile pilot|full_market` CLI、相同七條 feeds 與 calendar。
`FULL_MARKET_ENABLED` 只控制 full market 的新抓取資格，不影響安裝拓撲。

| 狀態 | 路徑 |
| --- | --- |
| Pilot | `/var/lib/findb-pilot/<environment>/state.sqlite3` |
| Staging Full | `/var/lib/findb-full-market/staging/state.sqlite3` |
| Production Full（保留既有路徑） | `/var/lib/findb-full-market/state.sqlite3` |
| 共用帳號 governor | `/var/lib/findb-account/<environment>/governor.sqlite3` |
| Historical request state | 原 provider `/var/lib/findb-*-fetcher/historical`，Twelve Data 為 `/var/lib/findb-fetcher/historical` |

三個 `findb-historical-*` workers 是獨立 transactional consumers，沿用既有人工歷史回補請求機制；
不再由 pilot 部署順便建立。所有 acquiring consumers 使用同一帳號 governor mount，保留 credential pins、
quota debt、unknown liability、cooldown 與 prepared delivery。不同環境仍使用各自帳號與儲存。
容器 UID/GID 為 10001，state directories `0700`、SQLite `0600`；provider credentials 不跨 consumer 注入。

部署前 Owner 必須停止兩個 profile，等待 desired／observed 都 stopped，取消或等待 historical／maintenance
acquiring work 完成。Idle daemon 仍顯示 running 是正常狀態；helper 會優雅停止並確認 exit 0、非 OOM、無 Docker error。
Backend API 以 `exec uvicorn` 接收停止訊號，dispatcher 使用 init 轉送訊號；raw cleanup
收到 TERM 後完成當前 transaction 並中斷每日等待。禁止用 `docker rm -f` 越過停止或 checkpoint 問題。交易保存原 container ID、image、名稱與 running state；
失敗或 INT／TERM／HUP 依反向順序恢復已接受的統一版 runtime。查詢失敗不代表不存在。

## 首次切換與 rollback 下限

首次切換先建立 SSM 設定、Secrets Manager configuration／TLS 及 main-ref IAM trust，驗證四組 schema。
先 staging，後以同 digests promotion production。每個環境都先人工停止並 drain 舊 runtime，保存 RDS snapshot／
可還原點、完整 SQLite＋sidecars、governor、raw marker、舊 current pointer 與 container inspection。
備份要能對應同一停止時點，並記錄 checksum；不得在 SQLite writer 還活動時直接複製單一主檔。

舊 pilot／full／historical containers 及 current pointer 在備份與停止證據完備後移出 active installation，
保留狀態目錄。新版 migration 保留 full plans、first dates，退休舊 pilot controls 與其 open alerts，
新 pilot controls 預設 stopped。原 full controls 不被自動 start。

新 manifest／acceptance 僅接受 schema version 3；Fetcher runtime profile 固定 `unified`。
舊 v1／v2 accepted replay、舊 pilot layout、舊主機路徑及 cutover 開關不再支援。
首次切換失敗時保持停止，以成套備份恢復，不能直接讓舊 image 操作新 schema／checkpoint。
成功後統一版本是 rollback 下限；schema migration 不提供 downgrade，日常 rollback 仍須通過 DB compatibility。

Rollback 入口與 promotion 相同，選擇 `mode=rollback` 和該環境已 accepted 的 exact bundle key。
它不重建 images、不修改 immutable acceptance、不刪 governor 或 prepared bodies。若 migration 不相容，採 forward fix，
或在停止狀態下進行已核對的完整備份復原。

## DB、credentials 與 network

Migration 與 application 使用不同 DB role。`reconcile_database_privileges.py` 驗證兩個 URL 指向相同 DB，
application role 不具 superuser／CREATEDB／CREATEROLE，授予 public／raw 所需 DML 與 sequence 權限及 default grants；
不授予 schema CREATE，alembic_version 只允許 SELECT。Migration secret 不得進入常駐 containers。

Deployment credential reconciliation 以 SHA-256 建立 queue-health Admin viewer、lookup Serve、static-cache Serve
固定 identity，輪替保留 audit；preflight check-only transaction rollback，migration／初始化階段才 commit。
Fetcher Source credential bridge 只跨 unit 傳送 hashes，不授予 instance role 跨 unit讀取 secrets。
四個 provider 的 Source scopes 必須涵蓋對應七條 feeds。統一 runtime 不再需要 Fetcher Serve calendar key。

RDS 不公開且只接受 FinDB security group；Fetcher 不連 RDS。Source 經 Cloudflare／nginx 還原可信 IP 後執行 allowlist。
nginx 非敏感設定在 `/etc/findb/nginx`，TLS、lookup key 在 `/run/findb-runtime-secrets/nginx`，
systemd 在 Docker 啟動前重建 tmpfs；缺失時 fail closed。Serve API 保持唯讀。

## 驗證與證據

交付前執行 `pnpm check`、`pnpm test`、`tofu fmt -check -recursive infra/tofu`，兩環境 `tofu validate` 與實際 refresh plan。
部署證據至少保留：release／digest、schema version、設定欄位驗證、IAM subject、備份、drain、migration revision、
SSM command 與終態、candidate health、immutable acceptance、activation health，以及兩 profile 缺漏隔離結果。
Native 資料驗收另列每條 feed 的實際結果與未解問題，見 [backlog](../dev/backlog.md)。

## 日曆政策設定切換

Backend `runtime/configuration` 加入四個獨立開關：`TWELVE_DATA_SKIP_NON_TRADING_DAYS`、
`FINLAB_SKIP_NON_TRADING_DAYS`、`SHIOAJI_SKIP_NON_TRADING_DAYS`、`TAIFEX_SKIP_NON_TRADING_DAYS`，
皆使用 `true`／`false` 字串且預設 `false`。
Fetcher `runtime/configuration` 移除 `FINDB_SERVE_BASE_URL`、`FETCHER_CALENDAR_TIMEOUT_SECONDS`、
`FETCHER_CALENDAR_CACHE_TTL_SECONDS`，不再讀取／注入 `FETCHER_CALENDAR_SERVE_API_KEY`；移除其 hash bridge 輸入。
舊 calendar credential metadata 標示 retired 並保留稽核，停止舊 runtime 後才移除作用中的依賴。
新部署載入器拒絕失效欄位，因此必須在對應環境維護停止後、部署新 candidate 前同步 AWS configuration。
政策只在 Backend 保存；Fetcher 經 Source 決策。兩環境部署均保持 controls stopped。
新版不能 downgrade 到沒有 frozen decision 的版本；失敗依成套備份復原，成功後以新版作 rollback 下限。
