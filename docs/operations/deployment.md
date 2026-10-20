# 部署、驗收與回退

本文描述兩個 AWS 環境共用的部署契約。Native 資料與實際 rollback 演練待辦見
[未完成驗收](../dev/production-foundation-cutover-plan.md)；部署健康通過不代表 native provider 問題已解決。

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
| Fetcher 四個 images、五個 providers | `fetcher-cd.yml`，main 自動部署 | `fetcher-production-cd.yml`，人工 promotion |

Staging build／reuse immutable commit images；production 複製 staging 已 accepted 的同一組 digests，不重新建置。
Production 的 annotated immutable tag 必須指向 main history 中的相同 commit，tag object、commit、bundle、
image digests 與 acceptance 全部一致。Backend 與 Fetcher 分別發布，跨 unit 變更採 Backend-first。
同 SHA 自動部署時，Fetcher 先等待 FinDB CD 結果；manual promotion 也必須遵循此順序。

Repo 僅保存 bootstrap 定位資訊。部署控制從 AWS SSM 讀取，應用設定從 Secrets Manager 的
`runtime/configuration` 讀取；credentials 維持獨立 secret。每個 consumer 的必填、選填與預設值在
staging／production 完全一致，只有 values 不同。設定驗證／同步見 [infra/env](../../infra/env/README.md)。
Runner 不讀取應用 secrets，由 instance role 在 host 以 tmpfs 載入最小 consumer scope。
`APP_ENVIRONMENT=local|staging|production` 是應用環境輸入；immutable JSON 的 `deployment_target` 必須吻合。

## Staging／production 流程差異

以下比較以 repository 的 workflow／部署腳本為準，不代表已即時查驗 Actions 執行紀錄或 AWS 設定值。

| 項目 | Staging | Production |
| --- | --- | --- |
| 觸發 | push main 後由 `change_policy.py` 判斷受影響 unit；也可人工 dispatch | 僅人工 dispatch，選擇 `promote` 或 `rollback`；push tag 不會觸發 |
| 版本選擇 | 完整 commit SHA；人工指定版本必須在 main history 中 | `findb-vMAJOR.MINOR.PATCH`／`fetcher-vMAJOR.MINOR.PATCH` annotated tag，指向 main history 中的 commit |
| 人工確認 | 手動新 candidate 要輸入 `deploy-new-candidate`；預設模式是 accepted replay | 必填 `PROMOTE_FINDB_PRODUCTION`／`PROMOTE_FETCHER_PRODUCTION`；不是 GitHub Environment 審批 |
| CI | 部署前執行指定 SHA 的 unit CI；自動入口依變更範圍選擇 | promotion 與 rollback 都在 tag binding 驗證後重跑指定 SHA 的 unit CI |
| 發布 images | 依 commit tag 建置，已存在則重用；部署固定 digest | 從 staging accepted bundle 複製 exact digests 至 production ECR，綁定版本 tag；不重建發布 images，CI 仍可能建置測試 images |
| 發布契約 | manifest 的 `release_tag` 為空；建立 staging candidate bundle | 額外保存 tag object／commit／staging accepted bundle 的 immutable binding；產生帶 production identity 與 promotion source 的新 bundle |
| 環境位置 | AWS account `439622209937`；`findb/staging/*` ECR 與 `/findb/staging/<unit>/deployment` SSM | AWS account `289112218471`；`findb/production/*` ECR 與 `/findb/production/<unit>/deployment` SSM；另需 staging reader 與 production promotion role |
| Accepted bundle key | `<unit>/accepted/<sha>/<bundle-sha256>.tar` | `<unit>/production/accepted/<sha>/<bundle-sha256>.tar`；images digest 相同不代表兩環境 bundle checksum 相同 |
| 回退 | 人工 `deployment_mode=replay`，指定該 SHA 的 staging accepted bundle 與相同 `image_tag` | 人工 `mode=rollback`，指定 production accepted bundle、對應 tag 與既有 binding；共用部署器以 `replay` 執行 |
| 跨 unit 順序 | Fetcher 的 push candidate 會等待同 SHA 的 FinDB CD 成功；手動 dispatch 沒有此等待 | 兩個 production workflows 獨立，Backend-first 由操作順序維持，沒有跨 workflow 自動等待 |
| 部署失敗回報 | 兩個 staging 入口皆有 failure／cancelled job，發布 `FinDB/Staging` 的 `DeploymentFailure` metric | production 入口沒有對應回報 job；IaC 雖定義 `FinDB/Production` 的同名 alarm，不能視為 workflow 失敗回報已接通 |

兩環境都在 `ap-southeast-1`，共用 `findb-deploy.yml`／`fetcher-deploy.yml` 和主機部署腳本。
兩者均執行 preflight、candidate 驗證、immutable acceptance 與 activation；replay 驗證既有 acceptance 後直接走 activation，
不建立新的 acceptance。RDS 備份與 DB compatibility、Secrets Manager consumer isolation、controls stopped、
checkpoint 完整性及 activation health 並未因 staging 而放寬。兩環境的 Full market checkpoint
路徑差異見下方持久化狀態表；schema-v3／統一 runtime 的 rollback 下限也同時適用。

## 部署耗時檢查（2026-10-16，優化前基準）

以下使用 GitHub Actions 的 job／step started_at、completed_at，整體時間以 run_started_at 至 updated_at
近似，包含編排與等待；不將平行 CI jobs 相加當成牆鐘時間。樣本限 2026-10-15～16 的成功部署，
排除文件變更導致的 skipped rollout、失敗執行與尚未完成的執行；不是 p95 或效能 SLA。
FinDB staging 取五次，Fetcher staging 取六次；production 各取目前統一版的一次成功 promotion。

| 流程 | 實測耗時 | 判斷 |
| --- | --- | --- |
| FinDB staging 全流程 | 11:22～12:38，中位數 11:49 | 合理，主要是 Backend CI 與兩階段主機部署 |
| FinDB Backend CI job | 4:29～5:42，中位數 5:03 | 合理；最新 2,201 tests 的 pytest 為 267.68 秒，20 分鐘 timeout 有充分餘裕 |
| FinDB migration CI job | 1:36～2:16，中位數 2:06 | 合理，與其他 CI jobs 平行 |
| FinDB Infra CI job | 1:43～2:38，中位數 2:31 | 合理，與其他 CI jobs 平行 |
| FinDB Dashboard CI job | 0:49～1:05，中位數 0:55 | 合理 |
| FinDB browser CI job | 2:21～2:45，中位數 2:43 | 合理，含 PostgreSQL 初始化與 hydration E2E |
| FinDB publish | 1:39～1:50，中位數 1:43 | 兩個 images 依序發布，目前不是主要瓶頸 |
| FinDB prepare | 0:17～0:22 | 合理 |
| FinDB 主機部署 job | 4:07～4:19，中位數 4:17 | preflight 約 20～23 秒、candidate 99～109 秒、activation 90～101 秒 |
| Fetcher staging 全流程，FinDB 沒有 rollout | 17:27～19:32，四次中位數 18:04 | 穩定但偏長，主要消耗在主機部署 |
| Fetcher staging 全流程，同 SHA 有 FinDB rollout | 26:11～27:26，兩次 | 額外等待 FinDB 約 9:16～9:17，且 image publish 也被等待阻擋 |
| Fetcher CI job | 2:08～4:21，中位數 2:26 | 合理，含 tests 與三個測試 images；15 分鐘 timeout 有餘裕 |
| Fetcher publish／prepare | 1:51～2:24／0:18～0:21 | 合理，三個 images 依序發布 |
| Fetcher 主機部署 job | 11:51～12:34，中位數約 12:26 | preflight 43～48 秒、candidate 221～241 秒、activation 413～435 秒；優先優化 |
| FinDB production promotion | 13:10 | CI 5:02、promotion 1:21、主機部署 5:56；單次樣本尚不能建立穩定基準 |
| Fetcher production promotion | 17:13 | CI 2:14、promotion 1:45、主機部署 12:24；瓶頸與 staging 一致，仍僅單次樣本 |

成功證據：FinDB staging [38836673961](https://github.com/FPI-TW/findb/actions/runs/38836673961)、
[38775214962](https://github.com/FPI-TW/findb/actions/runs/38775214962)、
[38720416747](https://github.com/FPI-TW/findb/actions/runs/38720416747)、
[38715581934](https://github.com/FPI-TW/findb/actions/runs/38715581934)、
[38706989093](https://github.com/FPI-TW/findb/actions/runs/38706989093)；
Fetcher staging [38824347083](https://github.com/FPI-TW/findb/actions/runs/38824347083)、
[38775214709](https://github.com/FPI-TW/findb/actions/runs/38775214709)、
[38769679416](https://github.com/FPI-TW/findb/actions/runs/38769679416)、
[38732467628](https://github.com/FPI-TW/findb/actions/runs/38732467628)、
[38725255170](https://github.com/FPI-TW/findb/actions/runs/38725255170)、
[38683048673](https://github.com/FPI-TW/findb/actions/runs/38683048673)。Production 使用下方復原基準的兩筆 runs。

## 部署耗時與失敗診斷調整

Runtime 優化適用於包含新版部署腳本的 release bundle；既有 accepted bundles 不覆寫，
回退舊 bundle 仍使用其原有 runtime 腳本。新版 protected-main driver 的等待／deadline 則適用於新版 workflow 發起的 replay。

Fetcher staging 先完成 images publish 與 candidate bundle upload，再等待同 SHA 的 FinDB CD 成功，
才輸出部署契約並進入主機部署。FinDB 失敗或找不到同 SHA 執行時仍拒絕 rollout；
手動 dispatch 維持既有操作順序。等待 job 預留 75 分鐘，其餘解析／binding、publish／promotion
分別設定 5、15 分鐘上限；FinDB bundle prepare 上限 10 分鐘。

Fetcher 每個 candidate／activation 階段各集中拉取、驗證三個 exact digests 一次。
專用 `images` consumer 不讀取 application／provider secrets；各 provider wrapper 仍獨立載入、清除
最小 credentials，並在使用前核對本機 RepoDigest。只有成功 activation、退休舊 containers 後
執行一次 image prune；清理失敗回報 warning，不影響已完成的 activation 身分。
Offline check、controls stopped、checkpoint 與 rollback journal 驗證均保留。
Loader 只允許 catalog 明確宣告 `secrets: []` 的 consumer 建立空 env 檔，仍套用 tmpfs、
owner／mode、exclusive create 與清理規則；宣告 secrets 的 consumer 若結果為空仍拒絕。

十個 recurring 與五個 historical runtimes 仍依序替換，原 container IDs／images／running state
保留到共同健康觀察通過後才清理。Coordinator 對全部新 runtimes 進行六次檢查，間隔五秒，
每次比對 ID、image、running 與 restart count；觀察窗口至少 25 秒，替代逐個等待造成的 275 秒。
任一檢查失敗則依原 journal 還原 containers 與 current pointer。這是程序存活檢查，
不代表 provider native 資料驗收或 controls 已 start。Standalone provider helper 仍保留原有 25 秒觀察。

FinDB candidate 執行 migration、DB grants／credentials reconciliation 與 registry provisioning。
Activation／accepted replay 改為 exact DB revision 與 credentials check-only，停止服務前即檢查相容性，
不再重跑 schema／registry mutations；不相容仍 fail closed。Production rollback 維持完整 unit CI，
本次未改成重用舊 CI 證據，也沒有新增成功 rollback 的實測基準。

兩個 reusable deploy job 上限由 90 改為 55 分鐘。Protected-main driver 負責主機 Bash wrapper 與 SSM wait，
避免 accepted replay 依賴舊 release 缺少的新 helper。主機 preflight／candidate／activation 分別由
GNU timeout 限制為 240／900／900 秒，TERM 後最多保留 180 秒供原停止／復原 traps 完成；
SSM executionTimeout 為 480／1200／1200 秒，runner wait deadline 為 540／1260／1260 秒。
Deadline、讀取失敗、terminal failure 或 runner 收到中止訊號都不產生新的 acceptance；runner 嘗試
cancel-command，最多另等 120 秒確認 SSM 終態。取消或 SSM terminal status 不能保證底層程序已停止，
仍須核對主機停止／復原證據；無法確認時明確回報 command ID，禁止直接重試部署。

2026-10-16 已透過 OpenTofu 套用兩環境 deploy role 的取消命令權限：

| 環境 | AWS account | 更新的 deploy roles | Apply 結果 | 套用後完整 refresh plan |
| --- | --- | --- | --- | --- |
| staging | `439622209937` | `findb-staging-deploy`、`fetcher-staging-deploy` | 0 新增、2 更新、0 刪除 | 無變更 |
| production | `289112218471` | `findb-production-deploy`、`fetcher-production-deploy` | 0 新增、2 更新、0 刪除 | 無變更 |

套用前逐項比對 saved plan，確認唯一差異為各 inline policy 新增 `CancelDeploymentCommand`
statement，既有權限不變。套用後重新讀取四個角色的線上 inline policy，均與計畫一致；
IAM Policy Simulator 對四個角色的 `ssm:CancelCommand` 驗證皆為 `ap-southeast-1` allowed、
`us-east-1` implicitDeny，且沒有缺少的 context values。本次未實際取消 SSM 命令或執行應用部署。

`ssm:CancelCommand` 沒有 resource-level IAM 支援，因此獨立 statement 使用 `Resource="*"`，
並以 `aws:RequestedRegion` 限制為 `ap-southeast-1`；SendCommand 仍限制各 unit 的主機。
參考 [AWS Systems Manager 授權表](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ssm.html)；
[AWS CancelCommand API](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_CancelCommand.html)
亦明定取消不保證底層程序終止。

[Fetcher 38836673928](https://github.com/FPI-TW/findb/actions/runs/38836673928) 於台北時間
2026-10-16 10:00:41 完成為 failure，總計約 16:57；candidate 在 46 秒後收到 SSM Failed／response code 2，
未保存 acceptance 或 activation。Owner 確認是 scheduler 仍在運行而阻擋部署。新版 unified CLI
會輸出以下受限格式，再以 code 2 停止，並提示先在 Dashboard 停止對應 scheduler：

```text
fetcher_deployment_blocked reason=scheduler_not_stopped execution_profile=full_market provider=twelve_data
```

SSM gate 只從失敗 stdout 擷取此固定 reason、合法 profile 與 provider，讓 Actions 顯示
`scheduler_not_stopped`、對應 profile／provider 及等待 desired／observed stopped 後重試的指引。
任意 stdout、stderr、Source 回應與 credentials 都不直接轉印；其他失敗保留安全通用訊息及 command ID。
Daemon container 為 running 且 idle 不等同 scheduler control 仍允許抓取，不能以 Docker running 直接阻擋部署。
舊 accepted images 沒有此 CLI 訊息，不會因更新 driver 而自動取得新版 diagnostic protocol。

[Fetcher 38843577455](https://github.com/FPI-TW/findb/actions/runs/38843577455) 於台北時間
2026-10-16 11:15:44 完成為 failure。CI、images publish、同 SHA FinDB CD 與 SSM preflight 通過，
但 candidate command `731e4e0a-e3cb-4014-af0e-7e51c77b1213` 在約 12 秒後以 code 1 失敗：
`runtime_secret_loader=failed unit=fetcher consumer=images reason=secret_schema_invalid`。
原因是 `images` 已宣告不讀 secrets，但寫入層仍拒絕空集合；失敗發生在 image pull 與 runtime
替換前，acceptance／activation 均未執行。2026-10-16 唯讀主機檢查確認 current pointer 仍指向
`74855de5e426a9eaea668481446957058ee980ee3b8a579fd2ed49fbfae9365b-38836673928-2`，
Pilot／Full checkpoint 檔案均存在；這不代表 provider 功能驗收。
修正必須進入新的 immutable release bundle 才會生效，重跑原 SHA 的既有 bundle 不會取得修正。
回歸測試涵蓋兩環境的 loader CLI、空檔安全寫入／check-only 清理、禁止讀 secrets，
並確認宣告 secrets 的空結果與不安全值仍被拒絕。
此修正已通過 585 項相關 Backend 部署測試、Infra 165 項測試（另含 268 個子測試）、
`pnpm check` 與 loader 的 Ruff lint／format check；尚未執行含此修正的 AWS 部署。

上方耗時表保留優化前樣本；固定 sleep 總額減少與可重疊建置時間只是預期改善，
部署後仍須以同 runner 規格的多次成功 Actions 重新量測，不能宣稱已達成新的總耗時。

部署耗時調整在修正空 env 問題前已通過 `pnpm check`、`pnpm test`、IaC format 與兩環境 `tofu validate`。
完整測試包含 Backend 2,203、migration 54、Infra 165（另含 268 個子測試）、Fetcher 1,406，
以及 Dashboard unit 192／browser 10／E2E 15。新增回歸涵蓋八種 profile／provider 診斷、敏感輸出隔離、
deadline 後不得 acceptance、取消／未確認終態、共同觀察窗口，以及健康失敗還原原 container IDs 與 pointer。
以上為本機驗證，不代表已部署或通過 AWS 線上 rollback 演練。

## 共用部署順序

排查「合併後失敗」時，先分開核對 PR 的 merged state、Required CI 與 main push 的 CD。
Fetcher CD 的 `Wait for same-revision FinDB staging rollout` 若回報
`FinDB rollout concluded failure`，代表上游 FinDB CD 失敗，須先追查該版本的 FinDB deploy job。
若 Actions 只有 `SSM terminal invocation failure` 與 `response_code`，以 log 中的 command ID、
target instance 和 region 唯讀查詢 `ssm get-command-invocation` 的 stdout／stderr；
必要時查對應 CloudWatch output。Exit code 本身不足以判定是 migration、configuration 或 health check。
重試前須確認 command 已終止與主機恢復狀態；未取得遠端輸出時，不宣稱已回滾或服務健康。

Backend image 以 `uv sync --frozen --no-dev --no-install-project` 安裝 runtime，只有 `/app`，
不包含 Backend dev group 的 editable `../fetcher`。部署 migration 直接執行 PATH 中的
`/app/.venv/bin/alembic`（`alembic upgrade head`、`alembic current`），不得裸用會重新處理
開發依賴的 `uv run alembic ...`，避免在執行 Alembic 前因
`Distribution not found at: file:///fetcher` 退出。驗證必須涵蓋實際 image 內的部署命令，
repo checkout 的 migration tests 與 image build 成功不足以覆蓋這個邊界。
此錯誤若發生在 `writers_stopped` 之後，candidate cleanup 會維持服務停止，不能視為已恢復舊版。

Staging replay 不執行 image publish；FinDB／Fetcher CD 的 deploy job 使用
`!cancelled() && needs.prepare.result == 'success'`，避免被略過的 publish ancestor 連帶略過，
並保留取消整個 workflow 時不繼續部署的行為。
恢復後須確認實際 deploy／activation job、服務與公開 endpoint，不能只看 workflow 的 success。

1. 解析指定版本與 AWS 設定，驗證 schema、account、instance tags／instance profile、SSM Online、
   registry、release manifest 與 exact image digests。RDS 需 available、備份保留至少一天，且可還原時間不超過一小時。
2. 以實際 image 執行 configuration、DB migration compatibility、credential reconciliation 的唯讀 preflight。
   SQLite checkpoint 必須存在且完整；已安裝 runtime 的 checkpoint 遺失不得被當成新主機初始化。
3. Backend candidate 停止公開 nginx、舊讀寫服務，優雅停止 writers；以 migration role 升級、核對 exact revision、
   校準 DB grants、deployment credentials 與 registry。兩環境執行相同步驟；不執行 calendar seed 或 completeness gate。
   Migration `91a4c7e2d630` 凍結既有工作使用的日曆與政策快照，完整保留既有 plans、first dates、prepared deliveries。
4. Backend candidate 只啟動內部 services，驗證 API、Dashboard、RabbitMQ durable topology、worker ping／heartbeat
   及 queue health；公開入口保持停止。成功後停止 candidate services，再回報可保存 acceptance。
   Fetcher candidate 執行 offline config、controls stopped 與 checkpoint 驗證，驗證最終 container config，
   不啟動未 accepted 的抓取程序。
5. Orchestrator 保存 immutable accepted bundle／record，綁定 commit、unit、target、digests、SSM command 與成功結果。
   SSM timeout、結果不明或缺少成功 marker 均不得視為 acceptance。
   Provider 安裝抑制 Docker image prune／pull 的例行 stdout，避免超過 SSM 回應上限而截斷終態 marker；
   錯誤 stderr 與失敗 exit code 仍保留，不以 CloudWatch 的成功文字繞過 acceptance gate。
6. Activate 再次驗證相同 identity、exact DB revision 與必要 gates，不重跑 migration／registry mutations；
   啟動 accepted services、公開 nginx，完成 TLS／routing／queue health，
   才更新 current pointer。Fetcher daemon 可保持 idle，抓取 desired state 仍由 Owner 人工控制。

日常 deployment 不執行 provider native 抓取 gate。不可因驗收而自動 start controls、重設首次日期，
也不可將 FinLab／Shioaji／Twelve Data／TAIFEX 或 R2 未完成的 native 驗收描述成部署成功的一部分。

## Runtime 與持久化狀態

每次 Fetcher 部署固定安裝十個 recurring runtimes：五個 `findb-pilot-*` 與五個 `findb-full-market-*`。
兩者使用同一 `findb-fetch-runtime --execution-profile pilot|full_market` CLI、相同七條 feeds 與 calendar。
`FULL_MARKET_ENABLED` 只控制 full market 的新抓取資格，不影響安裝拓撲。

| 狀態 | 路徑 |
| --- | --- |
| Pilot | `/var/lib/findb-pilot/<environment>/state.sqlite3` |
| Staging Full | `/var/lib/findb-full-market/staging/state.sqlite3` |
| Production Full（保留既有路徑） | `/var/lib/findb-full-market/state.sqlite3` |
| 共用帳號 governor | `/var/lib/findb-account/<environment>/governor.sqlite3` |
| Historical request state | 原 provider `/var/lib/findb-*-fetcher/historical`，Twelve Data 為 `/var/lib/findb-fetcher/historical` |

Historical Twelve Data／FinLab 以 Backend request／item lease 為持久狀態；Shioaji 另以 request UUID 命名 SQLite。
驗收應依實際 entrypoint 與 mount 判定使用中的檔案，不能把共用 helper 的未使用通用 state path 當成遺失 checkpoint。

五個 `findb-historical-*` workers 是獨立 transactional consumers，沿用既有人工歷史回補請求機制；
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
五個 provider 的 Source scopes 必須涵蓋對應七條 feeds。統一 runtime 不再需要 Fetcher Serve calendar key。

FinDB RDS 不公開且只接受 FinDB security group；Fetcher 不連 FinDB DB。FactSet consumer 另以 TLS 唯讀連接供應商 MySQL RDS，使用獨立 credentials。Source 經 Cloudflare／nginx 還原可信 IP 後執行 allowlist。
nginx 非敏感設定在 `/etc/findb/nginx`，TLS、lookup key 在 `/run/findb-runtime-secrets/nginx`，
systemd 在 Docker 啟動前重建 tmpfs；缺失時 fail closed。Serve API 保持唯讀。

## 驗證與證據

交付前執行 `pnpm check`、`pnpm test`、`tofu fmt -check -recursive infra/tofu`，兩環境 `tofu validate` 與實際 refresh plan。
部署證據至少保留：release／digest、schema version、設定欄位驗證、IAM subject、備份、drain、migration revision、
SSM command 與終態、candidate health、immutable acceptance、activation health，以及兩 profile 缺漏隔離結果。
切換的 SQLite 比對以備份建立基準：Full、governor 與每個 historical request 檔案均需存在且完整，
不能只檢查目前存活的檔案。保留原有資料表欄位與完整資料列，包含 prepared bodies、acquisition status／
actual start／login units／connections；quota、pacing 與 cooldown 不得倒退。新 Pilot checkpoint 首次建立，
需另核對 profile 身分、實際 mount 與完整性。停止期間不得意外新增 acquisition／work。
DB 另比對 plans、parts、members、first dates、admissions 與 historical requests／items 的既有資料列 fingerprints，
新 profile／calendar snapshot 欄位依 migration 契約單獨驗證。

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

## 目前部署與復原基準

2026-10-15 兩環境皆已 accepted 並 activated `121a6c36119af7182b0c5393a210be4de2485bdf`，
production 使用 `findb-v0.1.8`／`fetcher-v0.1.8`；DB revision 為 `91a4c7e2d630`。
此版本是新的 rollback 下限。兩環境各八個 acquisition controls 均保持 stopped；
八個 recurring runtimes 與三個 historical workers 正常 idle，不代表 Owner 已啟動抓取。

| 環境 | Backend acceptance／activation | Fetcher acceptance／activation |
| --- | --- | --- |
| Staging | [38683048580](https://github.com/FPI-TW/findb/actions/runs/38683048580) | [38683048673](https://github.com/FPI-TW/findb/actions/runs/38683048673) |
| Production | [38687240206](https://github.com/FPI-TW/findb/actions/runs/38687240206) | [38688560994](https://github.com/FPI-TW/findb/actions/runs/38688560994) |

五個 image digests 在兩環境相同，production 由 staging acceptance 複製而非重建：

| Image | Digest |
| --- | --- |
| Backend | `sha256:6379aa398db02e657c58178f2e671f565e1ee1d70fa5074915c6c2f7e6cf9f85` |
| Dashboard | `sha256:b1c1fcce4b79574fd3553ed9d1bcb2470a2c8ac463612e7bc16cec9473757f5c` |
| Twelve Data／TAIFEX | `sha256:03b7d0afe04feef4de98383460d6f9080f073976fc2fb11dbe65e1bbe38967d4` |
| FinLab | `sha256:fcdef402b897e8157a95c067b33f59decd8e490b918107ace9e54b7ef4d64bb6` |
| Shioaji | `sha256:379ed771e9a278caebdf8aaf0db45471930c488f9558101bc79a31991a4b2a7f` |

兩環境 Backend configuration 均有 24 欄、Fetcher 均有 12 欄，四個 provider skip 開關皆為 `false`。
四個舊 GitHub Environments 已移除；未再引用的 repository variables
`STAGING_ECR_CUTOVER_ENABLED`、`STAGING_INFRA_PLAN_ROLE_ARN` 也已移除，bootstrap 定位仍由 repo 檔案保存。
兩環境 IaC format／validate／refresh plan 通過；設定與 IAM 套用後 plan 均為 No changes。

部署後已核對 profile 分頁、預設 profile、獨立 plans／alerts、停止狀態、queue 與新心跳，
並查詢沒有 published revision 的年度，確認 weekday default 不自動發布。
Production 既有四個 plans、9,980 個 members、七個首次日期、admissions 與 23 個歷史回補 requests／30 個 items
在升級前後完整資料列 fingerprints 一致。兩環境 Full／governor／historical SQLite 均通過完整性與備份資料列比對；
production 13 個 historical SQLite、864 筆 acquisition intents 與既有 quota／pacing 保留。
新增 calendar snapshots 與 durable decisions 均已核對，不會以另一 profile 的最新 canonical run 改寫完成證據。

成套復原資料仍保留在各主機 `/var/backups/findb-unification/`，包括 checksum、SQLite sidecars、
container inspection 與 retired current pointer。以下快照均已完成，首次切換復原須成套使用；不能只啟動舊 image：

| 環境 | Backend／Fetcher 備份目錄尾碼 | RDS snapshot | Backend／Fetcher EBS snapshots |
| --- | --- | --- | --- |
| Staging | `staging-20261015T012544Z`／`staging-20261015T012452Z` | `findb-unification-staging-20261015-0138`；日曆 migration 前另有 `findb-calendar-staging-20261015-0329` | `snap-0cd2922180433d3c3`／`snap-03d20dd0f5ea05ae7` |
| Production | `production-20261015T052211Z`／`production-20261015T051820Z` | `findb-calendar-production-20261015-0515` | `snap-0fa74a2d5437a0953`／`snap-00570107475fbebb7` |

舊 shell PID 1 停止異常仍保留為稽核例外：staging ingest／raw cleanup exit 137；production
raw cleanup 確認僅 idle sleep 並凍結後終止，exit 137；production ingest 的 uvicorn 已完整 shutdown，
外層 shell exit 143。這些不記為優雅停止；停止後零活動 DB sessions、raw／durable work 未變與完整備份確認後才切換。
新版停止行為與備份檢查已修正。實際 production accepted-record rollback 演練與七條 feeds 的 native 資料驗收
仍依 [未完成驗收](../dev/production-foundation-cutover-plan.md) 列管，不因本次部署通過而結案。

## TAIFEX historical consumer 升級

Migration `a42c89d3e610` 為 `historical_backfill_item` 新增 nullable JSONB `result_evidence`，
不改既有資料；證據必須保留，rollback 採 forward fix，不能 drop column。先部署 Backend
migration／API，再部署 Fetcher。兩環境新增第四個 historical consumer
`findb-historical-taifex`，沿用 TAIFEX Source key／公開帳號 governor／Twelve Data image，
掛載 `/var/lib/findb-taifex-fetcher/historical`；不增加 provider secret。
Host preflight 依已驗證 immutable installed source 的 historical-taifex-v1 宣告決定必要 inventory：
舊版 11 consumers 可進行升級；新版必須具備 12 consumers，缺少新 worker 即拒絕。
Native 驗收應先以 staging 短區間及 production 一年邊界樣本驗證 raw、run／outbox、DQ、
canonical／Serve，再於維護窗口啟用正式回補；本機自動測試不代表雲端 native 驗收已完成。

## Provider work 切換

目前 Fetcher 包含 Shioaji checkpoint schema v10：已知 v9 以單一 SQLite transaction
放寬 audit numbering，保留全部原 rows、IDs、prepared bytes、receipts 與 credential pins；只有明確
predispatch `QUOTA_WAIT` 才排除 failure budget／rolling allowance，舊 unknown／failed rows 不重新分類。
Hard crash 在 commit 前會回復完整 v9；commit 後由 v10 reopen。升級前依下列 drain／備份程序保存
一致的 checkpoint 與 sidecars。舊 binary 會拒讀 v10；rollback 必須先停止並確認所有新 work 安全排空，
使用與舊 binary 相容且已核對原交付狀態的成套備份復原，不能改版號、刪 state 或遺失新 receipts。
若備份後已有交付，先 reconcile 全部新 immutable bytes／receipts 才能 rollback；account governor
usage／cooldown 不得退回較舊用量。Staging 已部署此版本；provider-work 啟用進度見下方 FactSet 章節。

`b53da90e7f21` 與 `c64eb01f8a32` 為 expand-only migrations（後者保存 provider account cooldown floor）。
Staging 已部署至 `d75fc12a9b43`，runtime 包含 provider-work-v1 protocol。
Owner cutover 與兩端 enforcement 的實際狀態見下方 FactSet 章節。
`infra/env/deployment.schema.json`、兩環境 deployment examples、Compose 與 Fetcher `.env.example`
均明示 `PROVIDER_WORK_REQUIRED=false`。Backend 與全部 Fetcher consumers 必須一起切換；
`FULL_MARKET_ENABLED` 是另一個獨立旗標，不能替代 queue cutover。完整邊界見
[provider work 架構](../architecture/provider_work.md)。

切換順序為可重複的安全契約：

1. 在舊 protocol 下停止新 acquisition，先送完所有已保存但尚未 accepted 的 prepared bytes，
   再讓所有已接受的 normalization terminal；舊 historical
   running item 必須完成或由 operator 取消。保存各 profile／historical SQLite checkpoint、raw bytes、
   prepared bodies、receipts、controls 與 DB scope，不刪除或重新初始化。未送出的舊 prepared 不得只因
   consumer 已退出就視為完成 drain；須以原 checkpoint 的實際 receipts／terminal 狀態對照。無法釐清舊外部 child 時停止切換。
2. 執行 expand migration。非 terminal historical／daily work 依 creation order 與日期匯入；原 lease token、
   request／plan／parts／member／calendar／raw 都不改寫。可明確對應 daily part／TAIFEX work 的既有 receipts
   綁定原 stable work；舊 Twelve Data／FinLab historical 缺少可驗證 linkage，不能猜測收據歸屬，須在舊 consumer
   下完成或取消。Legacy active owner 會留下 recovery blocker，TTL 不會解除。
3. 保持所有 consumer 停止，準備含 `com.findb.provider-work.protocol=v1` label 的新 images／containers，
   設定 `APP_ENVIRONMENT` 與 `PROVIDER_WORK_REQUIRED=true`，但尚不啟動 provider I/O。
   所有 profile／historical containers 必須 bind 同一 host 的 `/var/lib/findb-account/{environment}`。
   對每個 provider 執行新 CLI 的 `--initialize-state`，只建立／驗證原 profile state 與 shared lock inode；
   不得重建遺失的既有 checkpoint。保留單環境唯一 Fetcher host 的 trusted deployment inventory。
4. 保存完整 consumer `docker inspect` JSON（不可將含 secrets 的原始內容貼進 audit／chat），以及只包含
   該環境 Fetcher host IDs 的可信 inventory JSON。以 host local 的 `infra/deploy/provider_work_preflight.py`
   驗證單 host、全部 15 個 consumers、protocol flag／image label、graceful stopped state、同一 writable
   bind mount，並實際嘗試取得五個 provider flock。任一 orphan／未知 consumer／不同 inode 都拒絕。

   ```bash
   python3 infra/deploy/provider_work_preflight.py --environment staging \
     --host-id HOST_ID --host-inventory /run/findb/host-ids.json \
     --docker-inspect /run/findb/consumers.json > /run/findb/provider-work-proof.json
   ```

5. Owner 在五分鐘內將上述無 secrets proof 原樣 POST 至 Admin `/api/v1/admin/provider-work/cutover`。
   Backend 在固定 provider lock order 下再次驗證 environment、freshness、所有舊 runs terminal、
   沒有 running historical item／active queue owner；成功後保存具名 Admin audit、inventory SHA256 與
   shared inode identities，才解除 legacy blocker。不是手動確認「已停止」就可跳過檢查。
6. Backend 與全部新 consumer 的部署 runtime configuration 同步設定 `PROVIDER_WORK_REQUIRED=true`，
   啟動新 consumers。Source 未啟用前拒絕 queue claim；啟用後拒絕沒有 fence headers 的 legacy acceptance
   與 historical lease claim。從同一共享 inode 取得 lock 後，先恢復原 owner，再選下一 runnable FIFO。
   保留各 profile desired state、first dates 與 frozen plan。切換後 historical 不要求停止 Pilot／Full controls。
7. 驗證同 provider daily → historical → daily 的完整 canonical proof、不同 provider 並行、heartbeat-loss
   恢復原 owner、cancel／prepared drain，以及 Dashboard queue projection，才宣告該環境完成 cutover。

Rollback 也必須先停止 acquisition、證明 child reaped 並 drain accepted runs。不能在新 owner／prepared work
尚未排空時啟動舊 image，不能因 TTL 到期直接清 slot；expand tables／evidence 不 downgrade。

Provider-work 尚未啟用的 governor upgrade 另以 additive `full_account_cooldown` 保存 floor reason。
原 full_pacing／quota／intents／audits 不刪、不重設；舊資料只有 deadline 時保守作 legacy cooldown，
可能多一個安全 tail wait。明確正常 request pacing 維持原間距，不拆 SDK bootstrap。Upgrade/reopen
只延長 debt；rollback 仍須依上述完整停止／排空／成套checkpoint備份程序，不可倒退 usage／cooldown
或混用未通過此 dispatch gate 的 consumer。Runtime 部署成功不代表 provider-work queue 已啟用。

## FactSet 港股版本下限

新 Fetcher bundle v2 包含 FactSet 第四個 image、第五個 provider 與十五個 consumers。
OpenTofu 定義新增兩環境的 FactSet ECR repository、Source／provider secret metadata 與衍生
IAM repository scope；必須先完成基礎設施套用與填入 secret values，再部署新版 Fetcher。
2026-10-20 已在 staging 套用：4 個資源新增、5 個政策更新、零刪除；fresh plan 已無變更。
FactSet ECR 為 immutable，publisher 的 FactSet 查詢／推送權限已核對，FinDB 三份相關政策
保持原 scope。兩個 FactSet secrets 已初始化獨立 Source／RDS credentials，並完成
`fetcher-factset` Source registry 註冊（僅 `hk_equity_eod`、2400 requests／60 秒），
未輪替或修改其他 Source 身分。Fetcher host 的 runtime secret loader 與使用該 key
讀取 `market_eod.v2` 契約均已驗證。已補齊 `pilot_factset_v1` 與
`full_market_factset_v1` scheduler seed，均維持 `stopped`，其他排程未變更；
[Fetcher CD 39384380152，第 4 次執行](https://github.com/FPI-TW/findb/actions/runs/39384380152/attempts/4)
已完成候選驗收、immutable acceptance record 與正式啟用，部署 revision 為
`be09dde2fae5fd45b3d26a9e2950800b9ce34ed0`。主機唯讀核對十五個 consumers 均為
accepted、running、零重啟，image 符合 release manifest，三個 FactSet consumers 的
Source key 與本次 registry 註冊一致。
首次填值前須先核對 `AWSCURRENT`；已有版本時不得以初始化流程覆寫。Operator 僅將
Source key 的 SHA-256 傳到 FinDB application role；註冊須在同一 transaction 中鎖定
並重驗身分衝突、限定 scope、寫入 audit，遇到衝突或意外輪替即回滾。
初次 runtime 部署時維持 `FULL_MARKET_ENABLED=true`、`PROVIDER_WORK_REQUIRED=false`。
2026-10-20 的後續啟用已核對 59 筆 prepared request 的 checksum、Source 202 receipt 與 DB terminal，
十五個 consumers 均 graceful stop／exit 0。Checkpoint 成套備份位於兩台 staging host 的
`/var/backups/findb-provider-work/staging-20261020T1230Z`；Fetcher archive SHA-256 為
`cb80468e204c3b5aa296409b98cb56c5b2f2c6030d93552d94b49ae8d6d84829`，RDS snapshot
`findb-provider-work-staging-20261020-1230` 已 available。備份標籤不作實際擷取時間；
Fetcher 擷取於 12:20:28 UTC，RDS snapshot 建立於 12:20:39 UTC。

八份既有 SQLite 的原 tables／rows 比對一致，五組 shared-inode lock proof 通過，Owner cutover
已提交。兩端 AWS runtime config 與 deployment parameter 僅將 `PROVIDER_WORK_REQUIRED` 改為
`true`；舊 secret versions、原停止容器與 account usage 均保留。舊 Backend replay
[39422130758](https://github.com/FPI-TW/findb/actions/runs/39422130758) 在部署前因測試時鐘漂移失敗；
修正 [PR #322](https://github.com/FPI-TW/findb/pull/322) 已通過 Required CI 並整合。
[Backend CD 39426038217](https://github.com/FPI-TW/findb/actions/runs/39426038217) 已通過部署驗收，
revision 為 `8dc1f0a056ed3ed5f8828b410b6f56ee91a055fe`，四個 Backend roles 均已核對
`PROVIDER_WORK_REQUIRED=true`。[Fetcher CD 39429297439](https://github.com/FPI-TW/findb/actions/runs/39429297439)
已成功重播 schema-v3 accepted bundle，十五個 consumers 的 enforcement、零重啟與五組
attested inode 均核對一致。首次 FactSet maintenance 在實際 dispatch 前被取消；Source maintenance
授權有效，原 journal／budget 保留，三個 FactSet consumers 已 graceful stop 以診斷 dispatch
授權順序，另外十二個 consumers 與所有 stopped controls 不變。HKEX baseline、HK provider cutover
及原生行情驗收尚未完成。Production 基礎設施尚未套用。
舊 bundle v1 仍可供升級前安裝辨識；
新版 source bundle 與 topology marker 必須一致，promotion 必須含四個 immutable digests。

Backend expand migration `d75fc12a9b43` 保存 request-time HKEX cohort 與 partial 狀態；
`FACTSET_SKIP_NON_TRADING_DAYS`（預設 true）及 `EOD_LONG_GAP_TRADING_DAYS`（預設 5）
由 runtime/configuration 的 optional keys 注入。既有四個日曆開關的必填與預設規則不變。
Native 驗收、明確 HK provider 切換與 rollback 證據限制見 [FactSet runbook](factset_hk.md)。
