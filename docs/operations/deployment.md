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
| Fetcher 三個 images、四個 providers | `fetcher-cd.yml`，main 自動部署 | `fetcher-production-cd.yml`，人工 promotion |

兩個 production 入口只保留各自的 dispatch inputs、concurrency、unit CI、部署器與失敗回報；
tag 解析／binding 共用 `production-release.yml`，跨帳號 digest 複製與 production candidate bundle
共用 `production-promote.yml`，兩者以 `unit` 參數選擇 image 清單與 Fetcher 的 `migration_revision=none`／
`runtime_profile=unified` 規則。`release` 失敗（含輸入驗證與 binding 衝突）不發布 `DeploymentFailure`，
此時尚未變更 production。

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
| 觸發 | push main 後由 `change_policy.py` 比對同 workflow 最後一次成功 push run 的 SHA 判斷受影響 unit，找不到可信基準時全選；也可人工 dispatch | 僅人工 dispatch，選擇 `promote` 或 `rollback`；push tag 不會觸發 |
| 版本選擇 | 完整 commit SHA；人工指定版本必須在 main history 中 | `findb-vMAJOR.MINOR.PATCH`／`fetcher-vMAJOR.MINOR.PATCH` annotated tag，指向 main history 中的 commit |
| 人工確認 | 手動新 candidate 要輸入 `deploy-new-candidate`；預設模式是 accepted replay | 必填 `PROMOTE_FINDB_PRODUCTION`／`PROMOTE_FETCHER_PRODUCTION`；不是 GitHub Environment 審批 |
| CI | candidate 部署前執行指定 SHA 的 unit CI，自動入口依變更範圍選擇，tree 相同且 PR 已完整通過該 unit CI 時復用 PR 結果；accepted replay 不重跑 CI | promotion 在 tag binding 驗證後重跑指定 SHA 的 unit CI；rollback 只驗證既有 binding 與 production acceptance record，不重跑 CI |
| 發布 images | 依 commit tag 建置，已存在則重用；部署固定 digest | 從 staging accepted bundle 複製 exact digests 至 production ECR，綁定版本 tag；不重建發布 images，CI 仍可能建置測試 images |
| 發布契約 | manifest 的 `release_tag` 為空；建立 staging candidate bundle | 額外保存 tag object／commit／staging accepted bundle 的 immutable binding；產生帶 production identity 與 promotion source 的新 bundle |
| 環境位置 | AWS account `439622209937`；`findb/staging/*` ECR 與 `/findb/staging/<unit>/deployment` SSM | AWS account `289112218471`；`findb/production/*` ECR 與 `/findb/production/<unit>/deployment` SSM；另需 staging reader 與 production promotion role |
| Accepted bundle key | `<unit>/accepted/<sha>/<bundle-sha256>.tar` | `<unit>/production/accepted/<sha>/<bundle-sha256>.tar`；images digest 相同不代表兩環境 bundle checksum 相同 |
| 回退 | 人工 `deployment_mode=replay`，指定該 SHA 的 staging accepted bundle 與相同 `image_tag` | 人工 `mode=rollback`，指定 production accepted bundle、對應 tag 與既有 binding；共用部署器以 `replay` 執行 |
| 跨 unit 順序 | Fetcher 的 push candidate 會等待同 SHA 的 FinDB CD 成功；手動 dispatch 沒有此等待 | 兩個 production workflows 獨立，Backend-first 由操作順序維持，沒有跨 workflow 自動等待 |
| 部署失敗回報 | 兩個 staging 入口皆有 failure／cancelled job，發布 `FinDB/Staging` 的 `DeploymentFailure` metric | 兩個 production 入口在 CI／promotion／deploy 任一 failure 或 cancelled 時發布 `FinDB/Production` 的 `DeploymentFailure`（`DeploymentUnit=<unit>,Resource=github-actions`）；`release`（tag 解析與 binding）失敗不回報。每個 unit 一個 alarm，單一 datapoint 即告警、缺值視為正常；需在 production 套用 OpenTofu 後 alarm 才生效 |

兩環境都在 `ap-southeast-1`，共用 `findb-deploy.yml`／`fetcher-deploy.yml` 和主機部署腳本。
新版均執行 preflight、單次 apply、健康檢查、immutable acceptance 與 finalize；replay 驗證既有 acceptance，
仍需本次 transaction decision。舊 v3 bundle 維持 candidate／activation 協定。RDS 備份與 DB compatibility、Secrets Manager consumer isolation、controls stopped、
checkpoint 完整性及 activation health 並未因 staging 而放寬。兩環境的 Full market checkpoint
路徑差異見下方持久化狀態表；schema-v3／統一 runtime 的 rollback 下限也同時適用。

## 單次部署協定與五分鐘目標

新版 manifest／acceptance 使用 schema v4、`deployment_protocol=single-pass-v1`。一般增量部署的
目標為每個環境、每個 unit 低於 300 秒，包含 image build／push 或 promotion、編排、preflight、
主機更新、健康檢查及提交。Verify/test 與純跨 unit 等待另列；不能把主機健康檢查改名為 verify
來排除。冷快取、dependencies 大幅更新、大型 migration、故障復原及 v3 replay 另列實測。

流程為 `preflight → apply → 健康檢查 → runner acceptance → finalize`。Preflight 不重複拉取
images；apply 只更新一次服務。FinDB 在停止 writers 並核對 graceful shutdown 後執行 migration／
reconciliation，nginx mounts 就緒後只建立一次，保留 queue、worker、API、Dashboard、公開入口及
lookup key probe。Fetcher 以 provider 分組載入 credentials，同組 consumers 序列替換；八個
runtimes 共用至少 25 秒健康觀察。部署不變更 Owner desired controls、quota 或 checkpoint。

FinDB Backend／Dashboard images 及 production digest 複製平行執行，所有子程序成功才發布
bundle；各 publisher／deploy／promotion reader 的 IAM role 邊界不變。Staging selection 合併到
policy，仍須通過 CI 或可信 PR evidence；production verify 保留。Fetcher 仍遵守 Backend-first，
之後只對實際 restarting runtimes 有界等待，不固定空等 60 秒；OOM、未知身分或非正常退出仍阻擋。

### 持久化交接與復原

- `/opt/<unit>/transactions/<bundle-sha>-<run-id>-<attempt>/journal.json` 為 root-only、0600、原子
  fsync 的 journal，只記錄非敏感部署身分、original IDs、phase、deadline、current pointer 與健康結果。
- 主機修改前安裝 `findb-deployment-recovery-<unit>.timer`／`.service`，獨立於 SSM，每 15 秒巡檢，
  重開機後亦啟動。Apply 上限沿用 900 秒，watchdog 初始期限 1080 秒含 TERM 復原餘裕；健康後交接
  期限為 120 秒。Provisional containers 使用 `restart=no`。
- Runner 寫入 immutable accepted bundle／acceptance，另以條件寫入保存
  `<unit>[/production]/transactions/<transaction-id>.json`。Replay 也必須產生本次 decision，
  舊 acceptance 不能提交新 attempt。Instance role 只有讀取權，無新增寫入權。
- Finalize 與 watchdog 使用同一主機鎖，核對本次 decision、container IDs／images／啟動時間及健康，
  才更新 pointer、啟用 restart policy 並提交 journal；不重新啟動服務。已提交舊容器清理可重試。
- Runner 中斷或 acceptance 不可確認時，watchdog 停止未提交容器。Fetcher 還原原 runtime IDs 與
  pointer；FinDB 只在舊 release 的 exact DB revision 檢查通過後復原，絕不 downgrade schema。
  長時間 FinDB 復原亦持久化 child PID／boot identity，避免 SSM 中斷留下無主部署程序。
  不能復原或容器未 graceful shutdown 時記錄 `recovery_required`，不自動重啟異常關閉的舊 runtime；
  後續部署阻擋，需操作人員處理。
- 新 Fetcher 身分由 transaction label、root-only committed journal／receipt 共同證明；v3 的
  `accepted=true` label 僅供舊 runtime 相容。新 preflight 不接受未提交 transaction。

調查交接問題時，先核對 SSM command、journal phase、S3 本次 decision 與 current pointer。
可由 root 執行 release 內的 `python3 infra/deploy/deployment_transaction.py finalize --root <release-root>`
重試提交；`abort --root <release-root>` 會先確認是否已有有效 decision，存在則完成提交，否則復原。
不得刪除 journal／checkpoint 或重置 controls 來繞過未解決的 transaction。

### 版本相容與部署順序

V3 manifest／acceptance 及 immutable bundles 維持可讀，replay 使用其原 candidate／activation
腳本；不覆寫既有 evidence。Fetcher topology 的 `fetcher_bundle_version=3` 獨立於 manifest schema，
Shioaji 退休與統一 runtime 的 rollback 下限不變。Schema-v4 reader、host guard 與 promotion binding
驗證同步更新；各環境發布新版前，須先更新該環境 S3 bucket policy，將 transactions prefix 納入不可覆寫條件。

2026-11-09 09:55 UTC 已使用 OpenTofu 1.12.6 套用 staging 的 transactions 保護；production 的後續套用見下文。
來源為 `33d626b22dd82ef7ddfcfb21f5ddf1681c62149b`；完整 refresh saved plan 僅更新
`aws_s3_bucket_policy.deploy_bundle`，結果為 **0 新增、1 更新、0 刪除**。唯一 policy 差異是在既有
`DenyAcceptedBundleOverwrite` statement 增加 `findb/transactions/*` 與 `fetcher/transactions/*`，
沿用 `s3:PutObject` 的 `If-None-Match: *` 強制條件；HTTPS、KMS 與原 accepted 路徑規則保持不變。
套用的 saved plan SHA-256：
`e50b76d3cf855a75de7afa6f189f5ef97b20aef434cc1473e0860801a004f140`。

Refresh 同時更新三個 Fetcher IAM role 的既有 inline-policy 快照，反映先前 Shioaji 權限退役；
三個 role 的 planned actions 均為 `no-op`，本次沒有 IAM 權限寫入。套用後讀回完整 bucket policy，
確認與 reviewed plan 完全一致；再次完整 refresh plan 回報 **No changes**（exit code 0）。
本次只套用 staging bucket policy，未啟動應用部署，也不構成五分鐘部署驗收樣本。

2026-11-09 production 發布前，同樣使用 OpenTofu 1.12.6 執行完整 refresh saved plan，
唯一 planned update 為 `aws_s3_bucket_policy.deploy_bundle`：在 `DenyImmutableReleaseOverwrite`
新增 `findb/production/transactions/*` 與 `fetcher/production/transactions/*`，強制
`If-None-Match: *`。逐欄位確認其餘 policy 與 bucket attributes 完全相同，套用結果為
**0 新增、1 更新、0 刪除**；沒有 EC2、RDS 或 IAM 權限寫入。
Saved plan SHA-256：`8e834a073a98e33e1f77b638114909d41b6cd39872a8a601c8bd0378367efc0c`。
Refresh 另反映 RDS `latest_restorable_time` 與三個 IAM roles 的既有 inline-policy 快照，
這些資源 planned actions 均為 `no-op`。套用後讀回完整 policy 與 saved plan 一致，
再次完整 refresh plan 為 **No changes**。此 IaC 前置單獨記錄，不計為應用部署樣本。

Image prune 移至獨立 `Deployment image maintenance` workflow，每週一臺北時間 04:25 執行，也可
人工啟動；依 environment／unit 使用與 CD 相同 concurrency group，並取得主機 transaction lock。
只清除未使用且超過 168 小時的 images，有未解決 journal 時拒絕清理。Cache 生成及必要健康檢查
仍保留於部署計時內。

### 量測與驗收

四個 CD workflows 的 `deployment-summary` 上傳 `deployment-timing-<unit>` JSON 並寫入 Actions
summary：總牆鐘、僅 verify/test 阻塞、僅跨 unit 等待、部署淨耗時及各 job／step 階段。平行 jobs 不相加，重疊
部署工作的 verify／等待區間不扣除；超過 300 秒標示 warning，失敗紀錄也保留。起點為首個 job
開始，結束為部署 jobs 完成，初始 Actions queue 與 summary job 本身不列入。

初始預算為編排 45、建置／promotion 75、preflight 30、apply／health 130、commit 15 秒，共 295 秒；
此為目標，尚不是達標證據。11/9 重構前成功基準：FinDB staging 約 9:01、Fetcher staging 約
10:46（已扣純 FinDB wait）、FinDB production 約 6:31、Fetcher production 約 8:31（已扣 verify）。
來源 runs：42576454675、42576454674、42578616692、42579898821。

完成驗收需 staging、production 的兩個 units 各累積五筆一般增量成功部署，每筆低於 300 秒，
不得以重用同一 image 的 replay 代替一般增量樣本；冷建置與大型 migration 另外分類。合併前本機
測試不能取代這些 AWS 實測。Linux CI 額外執行真實 Docker 替換／復原及 systemd watchdog 測試。

### #422 staging 首輪實測與改善順序

2026-11-09 合併 revision `1b5a7fcaaf666a98ffd517f459a83b3e1e7414ee` 的兩次部署均成功。
Actions timing artifact 與 jobs API 時間一致；CloudWatch 同時確認 FinDB、Fetcher 的
`deployment_transaction=healthy` 與 `deployment_transaction=committed`。這是正常提交路徑的
實測證據，不代表取消、主機重啟、完整復原或 checkpoint／quota 不變的故障情境已驗收。

| 項目 | [FinDB staging](https://github.com/FPI-TW/findb/actions/runs/42619274513) | [Fetcher staging](https://github.com/FPI-TW/findb/actions/runs/42619274501) |
| --- | ---: | ---: |
| 首個 job 至部署結束 | 8:30（510 秒） | 12:34（754 秒） |
| 扣除 verify/test 的獨占區間 | 2:43（163 秒） | 3:32（212 秒） |
| 扣除純等待同 revision FinDB | 0 秒 | 2:52（172 秒） |
| 部署淨耗時 | **5:47（347 秒）** | **6:10（370 秒）** |
| 相較重構前基準縮短 | 3:14（約 36%） | 4:36（約 43%） |
| 距離嚴格低於 300 秒，至少還需縮短 | 48 秒 | 71 秒 |

這次五個 images 的 GHA cache 匯入與匯出皆出現 HTTP 400／HTML error response；匯出設定
`ignore-error=true`，所以發布仍成功。兩筆保留為「快取異常、部署成功但超標」樣本，不能當作
warm-cache 一般增量的代表，也不能刪除超標紀錄。截至 #422 該輪，staging 兩個 units 的合格一般增量樣本
均為 **0/5**；production 尚未新增驗收樣本。

下表按 Actions job／step 的秒級時間分解，所有列相加等於部署淨耗時；平行 images 取整個
build step 的牆鐘，不累加各 image。各 job 的 setup、post action、step 間隙與秒級取整差額
歸入該 job 的準備／收尾列。Job 間排程間隙仍計入部署。

| 步驟 | FinDB 秒 | Fetcher 秒 | 存在理由 | 改善方向 |
| --- | ---: | ---: | --- | --- |
| Policy／revision／CI gate | 8 | 6 | 驗證來源、main ancestry、candidate／replay 與 CI 證據 | 保留；不是主要瓶頸 |
| Publisher 準備與收尾 | 19 | 20 | Checkout、publisher OIDC、Buildx 初始化及清理 | 維持 publisher／deploy 權限分離；先處理 cache，再量測 Buildx 成本 |
| 平行 image build／push／digest 驗證 | 38 | 59 | 產生本 revision 的 immutable images | 優先修正 cache 版本／endpoint，明確記錄命中與匯出結果 |
| FinDB immutable migration revision | 13 | — | 從實際 backend image 執行 `alembic heads`，綁定 bundle | 在建置時產生與 digest 綁定的 revision metadata，避免 runner 再拉 image；保留 exact binding |
| Prepare 準備與收尾 | 17 | 14 | Selected source 與 protected-main driver checkout、deploy OIDC、AWS allowlist | 與同一 deploy role 的 deploy job 合併，保留兩種 checkout 的信任邊界 |
| Bundle 建立／上傳 | 5 | 4 | 封裝精確來源、manifest 與可信 validator | 保留 immutable bundle 與 hash；不為省秒跳過驗證 |
| Deploy 準備、release 驗證與收尾 | 25 | 23 | 再次設定帳號、role、driver，驗證並 materialize bundle | 合併 prepare 後重用同 job 設定；保留進主機前的 bundle／digest 驗證 |
| Read-only SSM preflight | 27 | 15 | 檢查主機、secrets、DB／既有 runtime eligibility | 批次唯讀 inventory、減少 CLI 啟動與輪詢延遲；檢查項目保留 |
| FinDB 完成後 Fetcher 即時恢復檢查 | — | 8 | 確認 12 個舊 runtimes 可安全替換 | 本次 `stable_seconds=0`，沒有固定 60 秒等待；可移入 apply 修改前，省一次 SSM 往返 |
| 單次 apply＋主機健康檢查 | 168 | 187 | 安全更新服務及確認可用，詳見下表 | 優先消除重複 secret 讀取、批次讀取 Docker metadata；保留停止 writers 與健康驗證 |
| Runner acceptance／本次 decision | 5 | 5 | Runner 為唯一 acceptance writer，條件寫入本次 transaction | 保留；不能重用舊 attempt 的 acceptance 代替本次提交 |
| SSM finalize | 9 | 15 | 同鎖核對 decision、commit pointer、啟用 restart policy、清理舊容器 | 減少 SSM 輪詢尾差；保留 durable commit／watchdog 競態保護 |
| Job 間排程間隙 | 13 | 14 | Actions job 交接 | 減少同 IAM scope 的 job 邊界；不把這段改列排除 |
| **合計** | **347** | **370** | | |

主機內部尚無完整逐階段計時。以下按 SSM CloudWatch stdout／stderr 的相鄰事件估算，可能包含
輸出緩衝與相鄰準備成本，不能視為單一 API 的精確效能；它們是上表 apply 的子區間，不再相加到總數。

| 主機子區間 | 約秒數 | 存在理由與改善方向 |
| --- | ---: | --- |
| FinDB：SSM 啟動、transaction 準備、credentials、pull | 29 | 取得精確 images、建立可復原狀態；保留 watchdog，減少重複 credentials 讀取 |
| FinDB：nginx materialize | 3 | 準備 allowlist、real-IP、Serve key 與 tmpfs；保留 |
| FinDB：migration／compose／credentials 載入及 DB／credential precheck | 30 | 在停止服務前拒絕不相容 DB 或 credential；兩個同 scope 的 one-shot checks 可合併 |
| FinDB：重新載入 compose credentials、停止 writers、核對退出 | 16 | 防止 migration 與舊 writers 同時執行；保留 graceful shutdown，重用同 transaction 的受限 compose scope |
| FinDB：再次載入 credentials、migration／reconciliation | 26 | 驗證 exact revision、權限、內部 credentials 與 registry；減少重複載入，migration credentials 不進長駐服務 |
| FinDB：再次載入 compose credentials、服務 up 至 nginx started | 36 | 按相依順序建立健康服務；檢視 health probe 的排程延遲，避免過早開放入口 |
| FinDB：nginx 啟動後完整健康、cache 與 journal healthy | 22 | Queue、worker、API、Dashboard、公開入口與 key 注入；先補各 probe 計時，再評估獨立 probe 平行化 |
| FinDB：結果傳回／runner 輪詢尾差 | 6 | 確認 SSM success marker；有界縮短輪詢間隔 |
| Fetcher：SSM、inventory／checkpoint、images 準備 | 43 | 記錄原 IDs、檢查 checkpoint 並取得三個 images；目前仍有逐欄位 `docker inspect` 與序列 pull，可批次 inspect、有限度平行 pull |
| Fetcher：Twelve Data 三 profiles | 30 | Provider credentials 隔離、offline／control 檢查、graceful stop、序列替換 |
| Fetcher：FinLab 三 profiles | 28 | 同上；合併同 scope 檢查程序，保持每個 profile 的控制與 checkpoint 檢查 |
| Fetcher：TAIFEX 三 profiles | 27 | 同上；批次讀取 metadata，容器修改仍序列化 |
| Fetcher：FactSet 三 profiles | 26 | 同上；不可為省時移除原容器完整復原能力 |
| Fetcher：共同健康觀察及 journal healthy | 27 | 12 個 runtimes 共用至少 25 秒，檢查 IDs、images、running、restart count；保留觀察下限 |
| Fetcher：結果傳回／runner 輪詢尾差 | 6 | 確認 SSM success marker；有界縮短輪詢間隔 |

### 耗時改善實作與後續驗收

Build helper 明確指定 GHA cache `version=2`，保留每個 image repository 的獨立 scope 與
immutable tag／digest 檢查。依 [Docker cache 版本選擇規則](https://docs.docker.com/build/cache/backends/gha/#version)，
這可避免由舊 `ACTIONS_CACHE_URL` 推導成 v1；下一筆 staging 必須確認五個 images 的 cache
匯入／匯出結果。服務端 cache 故障仍可能發生，不能把設定修正當作快取已命中的證據。

Runtime secret loader 先驗證整個 consumer allowlist，再以最多四個並行 AWS CLI requests
讀取該 consumer 的 secrets；所有讀取及 schema／distinct-key 驗證成功才產生 env。
任何一筆失敗均不啟動 command。各 provider wrapper、migration 與長駐服務的 credentials 邊界
保持隔離，不建立跨階段或跨部署 secret cache。這次先用有界平行讀取消除序列等待，避免為了
重用 credentials 引入額外的持久化與生命週期協定。FinDB 的 DB compatibility 與 credential
check-only 合併在同一短命容器，以 `sh -e` 確保前者失敗時不繼續後者。

Fetcher apply 的初始 inventory 以一次 Docker inspect 取得所需欄位，逐個驗證既有 runtime
與 checkpoint；這份快照只用於修改前檢查，替換、健康及復原仍讀取即時狀態。三個 exact images
平行拉取，各自核對 RepoDigest，等待全部子程序結束且成功後才修改容器。Provider 修改仍
序列化，保留十二個 runtimes 的共同至少 25 秒觀察與原 IDs 復原能力。

Staging 的 bundle preparation 移入 reusable deploy job，共用同一次 protected-main driver
checkout、AWS 設定與 deploy role；publisher 仍是獨立 job／IAM role。新增的 preparation input
只允許 main 的 staging candidate、空 bundle key 與空 release tag；production 與 replay 維持
傳入既有 bundle 的路徑。CI gate、故障告警、Backend-first 與 v3 相容皆保留。
SSM 正常完成輪詢改為兩秒，deadline、成功 marker、取消確認與 acceptance 規則不變。
主機 wrapper 額外輸出不含 secret 的 `deployment_runtime unit=... consumers=... seconds=... status=...`，
可區分 consumer 載入與命令的合計耗時；Actions 淨耗時計算維持原有規則。

#424 合併 revision `5bae57f2113e3a855e99ac4a303e3191999f8b85` 的首次 cache v2 部署均成功，
五個 images 的 cache 匯入／匯出不再出現 HTTP 400，首次匯出成功。兩個 workflows 均成功
重用 Required CI，verify/test 為 0 秒。這輪含冷快取建立成本，仍保留為超標樣本，不用暖快取
預估值取代實測。

| #424 實測 | [FinDB staging](https://github.com/FPI-TW/findb/actions/runs/42626688273) | [Fetcher staging](https://github.com/FPI-TW/findb/actions/runs/42626688275) |
| --- | ---: | ---: |
| 總牆鐘 | 322 秒 | 544 秒 |
| 排除純等待 FinDB | 0 秒 | 200 秒 |
| **部署淨耗時** | **322 秒（5:22）** | **344 秒（5:44）** |
| Policy | 9 秒 | 8 秒 |
| Publish（包含首次 cache 匯出） | 102 秒 | 83 秒 |
| Deploy（扣純等待） | 205 秒 | 248 秒 |
| Job 間隙 | 6 秒 | 5 秒 |
| Apply／health（已含於 deploy） | 140 秒 | 186 秒 |

相較 #422，FinDB 淨耗時減少 25 秒、apply 減少 28 秒；Fetcher 淨耗時減少 26 秒，但 apply
只減少 1 秒。Fetcher wrapper 實測 images 為 25 秒，四個 provider 依序為 26、26、29、27 秒；
不能宣稱平行 pull 或 secret 讀取已在這台主機取得明顯整體加速。截至 #424 該輪，一般增量合格樣本仍各為 0/5。
部署前後唯讀比對確認五個 SQLite databases 共 83 張表的 rows／SHA-256 完全一致，包含
checkpoint、quota 與歷史狀態；十二個 runtimes 均已替換，全部 running、restart count 0、
無 OOM，restart policy 為 `unless-stopped`。

已透過 #425 將 Fetcher 的即時恢復檢查放入 apply 的同一個 SSM command，位於 materialize、transaction
建立及任何容器修改之前；仍使用 protected-main driver 的腳本與 SHA-256 驗證，且只有等待
同 revision FinDB 的入口需要執行。保留 `stable_seconds=0`、180 秒恢復期限與失敗拒絕；
本機腳本遭竄改或恢復失敗都不會進入 apply。恢復與 apply 各自保留原 Bash 240／900 秒上限與 TERM cleanup；只有傳輸合併。
合併入口的 SSM／runner 上限採原兩段預算總和 1500／1620 秒；沒有恢復檢查時維持
原 SSM 1200 秒／runner 1260 秒，不延長任何主機階段的執行期限。
SSM gate 另記錄成功命令 elapsed seconds，方便和 Actions step 的往返成本對照。

Fetcher 等待同 revision FinDB 的 step 全部另列排除；恢復狀態、preflight、健康、cache 生成、
acceptance 與 finalize 仍計入。重用同一 SHA／image 的 replay 只能驗證相容與復原，不能補足
一般增量的五筆樣本。

### #425 staging 暖快取增量實測

2026-11-09 revision `c1c175b8a7496ff666472e3ee83ffcc9483f3756` 為新的部署腳本增量 candidate，
經 Required CI 通過後合併，未使用 replay；應用程式 layers 未變更，五個 images 都有 GHA
cache 命中且成功發布新 revision tag，沒有 HTTP 400。這筆證明暖快取下的部署腳本增量能達標，
尚不能代表應用程式 layers 改動、dependency 更新或冷快取的耗時。

| 實測 | [FinDB staging](https://github.com/FPI-TW/findb/actions/runs/42630986468) | [Fetcher staging](https://github.com/FPI-TW/findb/actions/runs/42630986350) |
| --- | ---: | ---: |
| 首個 job 至部署結束 | 261 秒 | 420 秒 |
| Verify/test 獨占區間 | 0 秒（重用 Required CI） | 0 秒（重用 Required CI） |
| 排除純等待同 revision FinDB | 0 秒 | 184 秒 |
| **部署淨耗時** | **261 秒（4:21）** | **236 秒（3:56）** |
| 距離 300 秒的餘裕 | 39 秒 | 64 秒 |
| 一般增量合格樣本 | **1/5** | **1/5** |

以下各列相加等於部署淨耗時，準備／收尾包含該 job 的秒級差額，job 間隙仍計入。恢復檢查
已包含在 Fetcher apply 的 128 秒，不因合併 SSM 或等待 FinDB 而排除。五個 images 平行建置
以 step 牆鐘計算，未累加個別 image 時間。

| 步驟 | FinDB 秒 | Fetcher 秒 | 存在理由 | 改善結果／後續方向 |
| --- | ---: | ---: | --- | --- |
| Policy／revision／CI gate | 9 | 9 | 驗證來源與可信 CI 證據 | 保留 |
| Publisher 準備／收尾 | 15 | 19 | 獨立 publisher IAM、checkout、Buildx | 保留權限分離；觀察初始化波動 |
| Image build／push／digest | 14 | 16 | 發布本 revision 的 immutable images | Cache v2 命中；上輪為 64／63 秒 |
| Immutable migration revision | 16 | — | 從實際 backend image 解析 exact revision | 後續可評估與 digest 綁定的 build metadata；本輪保留原檢查 |
| Deploy 準備／release 驗證／收尾 | 26 | 23 | Protected-main driver、deploy IAM、account 與 bundle 驗證 | Prepare 已併入同 IAM 的 deploy job |
| Bundle 建立／上傳 | 4 | 5 | 精確來源、manifest、hash 與可信 validator | 保留 |
| Read-only SSM preflight | 20 | 14 | 修改前檢查主機、secrets、DB／runtimes | 保留所有拒絕條件；使用較短正常輪詢 |
| Apply／恢復／主機健康 | 135 | 128 | 安全更新、migration、即時恢復 gate、服務與公開入口健康 | 消除重複管理容器與 SSM 往返；保留 provider 序列修改及至少 25 秒共同觀察 |
| Runner acceptance／decision | 6 | 6 | 本次 transaction 的唯一 runner acceptance | 保留 immutable／條件寫入 |
| Finalize | 10 | 10 | 同鎖驗證 decision、pointer、restart policy 與清理 | 保留 watchdog 競態保護 |
| Job 間排程間隙 | 6 | 6 | Actions job 交接 | 保留於計時內 |
| **合計** | **261** | **236** | | |

Fetcher 的主機 wrapper 實測 images 2 秒，Twelve Data／FinLab／TAIFEX／FactSet 各為
18／20／18／19 秒；這些是 apply 的子區間，不重複加總。相較 #424，淨耗時減少 61／108 秒；
同時存在暖快取與主機 image layers 重用，不能將所有改善歸因於單一程式變更。
兩個主機皆回報本次 `deployment_transaction=committed`。部署前後唯讀快照確認五個 SQLite
databases 共 83 張表的 rows／SHA-256 完全一致，checkpoint、quota 與歷史工作狀態未變。
十二個 runtimes 的原 IDs 均已替換，全部 running、restart count 0、無 OOM，restart policy
為 `unless-stopped`，transaction label 與本次 run 一致。

[Required CI](https://github.com/FPI-TW/findb/actions/runs/42630201966) 已通過 Backend、Fetcher、
Dashboard、完整 Infra（965 tests、289 subtests 通過），並執行真實 Docker／systemd recovery。
本機完整 `pnpm check`、受影響部署測試與 timeout／guard 組合測試亦通過。
本輪成功不代替 AWS 取消、主機重啟或完整故障注入演練。

截至 #425 staging 實測，兩個 units 各一筆合格增量，尚未完成各五筆驗收；後續須保留真實應用程式
增量、超標及失敗樣本，不能以空 commit 或同 SHA replay 補足。Production 後續 promotion
與實測見下節。
尚未實作的 image digest 綁定 migration metadata、細部 health probes 平行化列為後續候選，
先累積實際增量的分布與瓶頸證據，不縮短安全／健康觀察下限。

### Production 單次部署首輪實測

2026-11-09 依人工 promotion 入口發布 staging 已驗收的
`c1c175b8a7496ff666472e3ee83ffcc9483f3756`，版本為 `findb-v0.1.21`／`fetcher-v0.1.27`。
先確認 FinDB workflow 與本次 transaction committed，再啟動 Fetcher；沒有並行更新兩個 units。
Production verify 全部通過，五個 production ECR digests 與 staging accepted manifests 一致。
兩個 schema-v4 accepted bundles、acceptance 與本次 SSM finalize committed marker 均已獨立核對。
DB revision 維持 `d8e3f0b5a2c4`，未 downgrade 或重啟 Owner controls。

| 實測 | [FinDB production](https://github.com/FPI-TW/findb/actions/runs/42637892729) | [Fetcher production](https://github.com/FPI-TW/findb/actions/runs/42639305431) |
| --- | ---: | ---: |
| 首個 job 至部署結束 | 448 秒 | 491 秒 |
| 排除 verify/test 獨占區間 | 211 秒 | 185 秒 |
| Workflow 內純跨 unit 等待 | 0 秒 | 0 秒（FinDB 完成後才 dispatch） |
| **部署淨耗時** | **237 秒（3:57）** | **306 秒（5:06）** |
| 五分鐘判定 | 通過 | 超標 6 秒 |
| Production 合格一般增量樣本 | **1/5** | **0/5** |

Fetcher 開始前等待 FinDB 的時間不在其 workflow 時間範圍內，沒有再次扣除；promotion、
preflight、所有主機健康檢查、acceptance 與 finalize 仍計入。Fetcher 這筆是部署成功但超標，
完整保留，不用 replay／重跑替代，也不宣稱 production 已全面達成五分鐘。

| 步驟 | FinDB 秒 | Fetcher 秒 | 存在理由與改善方向 |
| --- | ---: | ---: | --- |
| Release resolve | 5 | 8 | 驗證 annotated tag、exact revision 與 main ancestry；保留 |
| Release binding | 28 | 25 | 核對 staging acceptance，綁定 immutable tag／bundle；保留 IAM 分離與條件寫入 |
| Promotion 準備／收尾 | 33 | 36 | Protected driver、跨帳號 roles、來源 bundle 驗證、Buildx；後續量測重複初始化成本 |
| 平行 digest 複製／candidate bundle | 25 | 39 | 複製 exact digests 並核對目的端與 immutable tag；保留全部成功才發布 |
| Deploy 準備／materialize／收尾 | 28 | 27 | Account、role、driver、release 驗證；維持權限邊界 |
| Read-only preflight | 8 | 12 | DB／主機／secrets／checkpoint／既有 runtime eligibility；保留 |
| Apply／主機健康 | 86 | 135 | Writers graceful shutdown、exact migration、runtime 替換與完整健康；保留所有拒絕條件及至少 25 秒觀察 |
| Runner acceptance | 5 | 5 | 本次 immutable acceptance／decision；保留 |
| Finalize | 6 | 6 | 同鎖 commit、restart policy、清理原容器；不重新部署 |
| Job 間排程間隙 | 13 | 13 | 交接成本仍計入，不能改列 verify 排除 |
| **合計** | **237** | **306** | |

Fetcher wrapper 子區間為 images 20 秒，Twelve Data／FinLab／TAIFEX／FactSet 各
17／16／16／16 秒；已含於 apply，不重複加總。後續優先觀察 promotion 準備與 digest 複製的
波動，再評估可合併的同權限初始化；不為補足 6 秒而刪減健康觀察或放寬安全檢查。

部署前後五個 SQLite databases 共 83 張表的 rows／SHA-256 完全一致，包含 production
既有根路徑 `/var/lib/findb-full-market/state.sqlite3`、Pilot、account governor、provider-work
及 TAIFEX historical state。Checkpoint、quota 與歷史工作狀態未改寫。十二個 runtimes
原 IDs 均已替換，全部 running、restart count 0、無 OOM，restart policy 為 `unless-stopped`；
images、transaction labels 與 current pointer 均符合本次 committed release。

Production accepted bundles：

```text
findb/production/accepted/c1c175b8a7496ff666472e3ee83ffcc9483f3756/ff21ee6931d1d8066dfde8b3a9ddd428aa43b003c38f87ab1551e2161e9f7b23.tar
fetcher/production/accepted/c1c175b8a7496ff666472e3ee83ffcc9483f3756/1cac68892f4e3b9263579fc237fd1ce0825be1927dcc04208557ff7bd3140880.tar
```

首輪後 production 又累積以下一般增量 promotion 樣本，均已扣 verify，Fetcher 於 FinDB
committed 後才 dispatch，workflow 內無跨 unit 等待：

| Release | Unit | Run | 淨耗時 | 總牆鐘 | Verify |
| --- | --- | --- | ---: | ---: | ---: |
| `findb-v0.1.22`（`077ee02`） | FinDB | [42656213012](https://github.com/FPI-TW/findb/actions/runs/42656213012) | **229 秒** | 453 秒 | 224 秒 |
| `findb-v0.1.23`（`d86b633`） | FinDB | [42742153828](https://github.com/FPI-TW/findb/actions/runs/42742153828) | **245 秒** | 453 秒 | 208 秒 |
| `fetcher-v0.1.28`（`d86b633`） | Fetcher | [42743102997](https://github.com/FPI-TW/findb/actions/runs/42743102997) | **280 秒** | 509 秒 | 229 秒 |
| `findb-v0.1.24`（`8104e48`） | FinDB | [42767771254](https://github.com/FPI-TW/findb/actions/runs/42767771254) | **240 秒** | 466 秒 | 226 秒 |

Production 合格樣本為 FinDB 4/5、Fetcher 1/5；staging 計數不在本表更新範圍。
四組各五筆的整體驗收尚未完成。本次正常部署成功不替代 AWS 故障注入、取消、主機重啟與
正式 rollback 演練；既有未完成資料與復原驗收繼續列管。

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
Host scripts 的已處理失敗都以固定 `<component>=failed reason=<token>` 行結束，多半寫在 stderr。
SSM gate 同時讀取 stdout 與 stderr，只轉印完全符合此文法的行（已知 component、小寫 reason token，
以及選用的 `runtime=`／`container=`／`service=` 名稱或 `committed=true`），依首次出現順序去重、
最多 8 行，並以 `遠端部署失敗原因` annotation 顯示；不符文法的 Docker 或 provider 文字一律不輸出。
Host eligibility 拒絕某個已知 consumer 時，trusted guard 另輸出受限的
`fetcher_host_blocked reason=runtime_not_eligible execution_profile=pilot provider=twelve_data`。
SSM gate 只接受合法的 `pilot|full_market|historical` 與五個 provider，於 Actions 提示檢查
既有 consumer、保持 control 停止並安全復原原 owner。重啟循環、非零 exit、identity 或
argv 不符仍拒絕部署；診斷不直接輸出 Docker Error、environment 或原始 stdout／stderr。
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

## Source 暫停修正的部署驗收

2026-10-30 的 Fetcher 修正 revision 為 `c4effb8a64ef842dfa8aa9ed164244a852e7290a`
（[PR #379](https://github.com/FPI-TW/findb/pull/379)）。
[Staging Fetcher CD](https://github.com/FPI-TW/findb/actions/runs/40989530773) 成功後，
核對 schema-v3 acceptance、bundle SHA-256、installed manifest 與四組 image digests 一致；
十五個 Pilot／Full／historical consumers 均 accepted、running、零重啟，無新 traceback。
Staging 佇列 API 回傳 HTTP 200；Pilot／Full、historical、account governor 與 work journal
的 SQLite 完整性檢查均為 `ok`。本次 FinDB CD 只有政策檢查，Backend／Dashboard rollout
未被選中，production 也只 promotion Fetcher。

在 staging 四組已安裝 images 內，以 `httpx.MockTransport` 執行共 72 項離線檢查：
408／429／503／521／522／524 在 bounded inner retries 後由 idle loop 等待並恢復，
401／403 立即失敗，已設定的停止旗標阻止重試，
active owner 的 Source uncertainty 仍越過一般 Exception handler。四組 images 的
`source_recovery.py` SHA-256 都與合併版本一致。此驗證不切斷真實 Source，亦不呼叫 provider。

Production 升版前，以 Owner API 正常停止原本啟用的 Full Shioaji（revision 12→13）
與 TAIFEX（10→11），確認所有 controls desired stopped、五個 provider 無 active owner。
保存 PostgreSQL prepared hashes／receipts，以及主機 SQLite tables 的逐表指紋、檔案 inode
與五組 provider host lock identity；不得以重設 checkpoint、refund 舊失敗或清空 cache 通過部署。
本次部署驗收不代表 Shioaji 空 Kbars／mapping 問題（[issue #378](https://github.com/FPI-TW/findb/issues/378)）
已修復；Shioaji 退役後該 issue 未經 production 驗收即關閉，保留證據與權限見
[Shioaji Backend 退役部署基準](production.md#shioaji-backend-退役部署基準)，分 K 缺口由
backlog 的替代 provider 項目追蹤。

[Production Fetcher CD](https://github.com/FPI-TW/findb/actions/runs/40992266165) 於
`2026-10-30 02:45 UTC` 成功完成 `fetcher-v0.1.13` promotion；指定 revision 的 Fetcher CI
通過 1,811 項測試，production 四組 digests 與上述 staging acceptance 相同。正式 bundle 為：

```text
fetcher/production/accepted/c4effb8a64ef842dfa8aa9ed164244a852e7290a/bcfd723306836fa7cf84a3dbb290f1857e172bc1aaa8e6795e3e6429803c61d1.tar
```

Activation SSM 為 `c1bf33c2-8a69-408b-b45f-a2caf9d6a5ac`。升版後四組 installed images
再次通過上述 72 項離線檢查；十五個 consumers accepted／running／零重啟，並以 trusted driver
同版 recovery helper 確認 container ID、image、start time 與 restart count 連續 60 秒不變。
Helper 不包含在 runtime bundle，獨立主機驗收須傳入可信同版 helper，不能假設 release 目錄已有該檔。

恢復 controls 前，逐表比對 18 個既有 SQLite 狀態庫的 141 張表，內容指紋全部相同，
檔案 inode 與 19 個既有 lock inode 均保留（含五組 provider whole-work lock）。
PostgreSQL 的 110 筆 work／prepared hashes、attempt counts、owner generations、
19,597 筆 receipts 的數量與完整內容指紋亦一致，Backend manifest 未變更。
Governor quota 共 54,573 個 windows、1,629,066 requests，work budget 共 2 筆／2 attempts；
全部 account quota、cooldown、acquisition liability 與各層 retry budget 逐表指紋均未變更。
主機穩定／保留性驗收 SSM 為 `dd88a2d4-2cb3-4118-ba04-d3cd37537c54`。
保留性驗收通過後，Owner API 已恢復 Full Shioaji（13→14）與 TAIFEX（11→12）的
`desired_state=running`；TAIFEX 當時的 `observed_state=stopped` 表示 cycle 閒置。
其餘 controls 的 desired state 與部署前相同，全部 heartbeat 更新且 `last_error` 為空。
Production 供應商佇列 API 回傳 HTTP 200，原有 26 筆 queued work 的 ID 集合保留，
其中 Shioaji 五筆工作繼續遵守原抓取窗口與 retry budget。
本次未透過切斷線上 Backend 或實際 provider I/O 進行故障注入，不取代 native／active-owner recovery 驗收。

## 共用部署順序

變更範圍不以 `github.event.before` 計算：被後續 push 取代而取消的 pending run、或部署失敗的 run，
其變更會由下一次 push 併入比對，不會因下一個 commit 只改文件而漏部署。`fetcher/` 與所有 `infra/`
變更都會觸發 FinDB CI（Backend tests import Fetcher 並檢查 infra 檔案）；`.github/actions/` 變更
同時觸發兩個 unit 的 CI 與 staging rollout。

Push 入口在 change policy 選到某 unit 的 CI 時，先以 `infra/deploy/ci_evidence.py` 查詢可復用的 PR CI：
合併 commit 必須對應唯一、同 repo、base 為 main 的已合併 PR，其 tree 與 PR head 完全相同；該 head 最新一次
`Required CI` 必須成功，且該 unit CI workflow 的每個 job 於最新 attempt 皆為 success（PR 略過該 unit 時不算）。
符合時跳過 `verify` 並在 log 輸出 `ci_evidence=reused` 與 PR／run ID；任何不符或 API 錯誤都輸出
`ci_evidence=unavailable reason=...` 並照常執行 CI。手動 dispatch 與 production promotion 不使用此復用。
`verify` 可能被跳過，因此其下游 job（`publish`、`deploy`）的 `if` 都必須含
`!cancelled()` 或 `always()`，否則隱含的 `success()` 會因上游 skipped 而連帶略過部署。

排查「合併後失敗」時，先分開核對 PR 的 merged state、Required CI 與 main push 的 CD。
Fetcher CD 的 `Wait for same-revision FinDB staging rollout` 若回報
`FinDB rollout concluded failure`，代表上游 FinDB CD 失敗，須先追查該版本的 FinDB deploy job。
若 Actions 只有 `SSM terminal invocation failure` 與 `response_code`，以 log 中的 command ID、
target instance 和 region 唯讀查詢 `ssm get-command-invocation` 的 stdout／stderr；
必要時查對應 CloudWatch output。Exit code 本身不足以判定是 migration、configuration 或 health check。
重試前須確認 command 已終止與主機恢復狀態；未取得遠端輸出時，不宣稱已回滾或服務健康。

若 candidate 回報 `writer_shutdown_not_graceful`，cleanup 會維持 application containers 停止，
不會自動恢復服務。先核對遠端輸出確實停在 migration 前、`current` 仍指向原 accepted release，
且各容器的 image digest 與原 manifest 一致；符合時可重新啟動這些既有容器恢復原版，驗證健康、
具認證的 Serve 查詢、停止的 controls 與資料指紋後，再重跑失敗的 deploy job。
不得修改容器 exit code 或跳過 graceful-shutdown gate；若已開始 migration 或映像不符，須改走
對應的復原流程。Serve 停止時若持續等待 background tasks，保留其 shutdown log，恢復後重測原本
逾時的查詢，不能只用 `/health` 判定查詢已恢復。

Backend image 以 `uv sync --frozen --no-dev --no-install-project` 安裝 runtime，只有 `/app`，
不包含 Backend dev group 的 editable `../fetcher`。部署 migration 直接執行 PATH 中的
`/app/.venv/bin/alembic`（`alembic upgrade head`、`alembic current`），不得裸用會重新處理
開發依賴的 `uv run alembic ...`，避免在執行 Alembic 前因
`Distribution not found at: file:///fetcher` 退出。驗證必須涵蓋實際 image 內的部署命令，
repo checkout 的 migration tests 與 image build 成功不足以覆蓋這個邊界。
此錯誤若發生在 `writers_stopped` 之後，candidate cleanup 會維持服務停止，不能視為已恢復舊版。

Schema reconciliation migration `1e68a0c9b3d2`（前版 `0d57f9b8a2c1`）將 16 個重複前綴的
CHECK 改名，保留其定義與 OID，並新增 `calendar_market.market` 的格式約束
`^[A-Z][A-Z0-9_]{1,9}$`。此格式允許新增市場，並非固定市場清單。升級會驗證既有資料；
可在升級前唯讀查詢 `SELECT market FROM calendar_market WHERE market !~ '^[A-Z][A-Z0-9_]{1,9}$'`。
若有無效代碼，升級會完整回滾，須釐清資料與引用關係後修正再重試；migration 不改寫或刪除資料。
DDL 會取得相關 tables 的 locks；沿用停止 writers 後執行 migration 的部署程序。
`trading_calendar.created_at`／`updated_at` 的 server defaults 改為 timestamptz `now()`，
避免非 UTC session 將舊 `timezone('utc', now())` 的無時區結果錯誤轉換；既有時間戳不改寫。
ORM 同步宣告其餘既有 DB defaults，兩種建表方式維持相同預設值。
此 revision 可降回前版，恢復舊 CHECK 名稱及時間戳 defaults、移除新增 CHECK，保留資料；
不改變既有 rollback 下限，也不代表較舊 accepted image 可略過 DB compatibility 檢查。

Staging replay 不執行 image publish；FinDB／Fetcher CD 的 deploy job 使用
含 `!cancelled()` 的明確條件，允許 policy 成功、mode 為 replay 且 publish skipped 的組合，
避免被略過的 publisher 連帶略過 deploy，並保留取消整個 workflow 時不繼續部署的行為。
恢復後須確認實際 deploy／activation job、服務與公開 endpoint，不能只看 workflow 的 success。

1. 解析指定版本與 AWS 設定，驗證 schema、account、instance tags／instance profile、SSM Online、
   registry、release manifest 與 exact image digests。RDS 需 available、備份保留至少一天，且可還原時間不超過一小時。
2. 以實際 image 執行 configuration、DB migration compatibility、credential reconciliation 的唯讀 preflight。
   SQLite checkpoint 必須存在且完整；已安裝 runtime 的 checkpoint 遺失不得被當成新主機初始化。
3. Backend candidate 停止公開 nginx、舊讀寫服務，優雅停止 writers；以 migration role 升級、核對 exact revision、
   校準 DB grants、deployment credentials 與 registry。兩環境執行相同步驟；不執行 calendar seed 或 completeness gate。
   Migration `91a4c7e2d630` 凍結既有工作使用的日曆與政策快照，完整保留既有 plans、first dates、prepared deliveries。
4. Schema-v4 Backend apply 只更新一次服務，驗證 API、Dashboard、RabbitMQ durable topology、worker
   ping／heartbeat、queue health、公開入口與 Serve key 注入。Fetcher 在 offline config、controls
   stopped 與 checkpoint 驗證後序列替換各 runtime，保持 controls 停止，完成共同健康觀察。
5. Runner 保存 immutable accepted bundle／record 及本次 transaction decision，綁定 commit、unit、
   target、digests、SSM command 與成功結果。SSM timeout、結果不明或缺少成功 marker 均不得視為 acceptance。
   Provider 安裝抑制 Docker image pull 的例行 stdout，避免超過 SSM 回應上限而截斷終態 marker；
   錯誤 stderr 與失敗 exit code 仍保留，不以 CloudWatch 的成功文字繞過 acceptance gate。
6. Finalize 在主機鎖內核對本次 decision 與 runtime identity，提交 current pointer、啟用 restart
   policy 並清理舊容器，不重新部署。Fetcher daemon 可保持 idle，抓取 desired state 仍由 Owner 人工控制。
   舊 schema-v3 bundle 的 replay／rollback 保留其 candidate／activation 路徑，不套用五分鐘承諾。

日常 deployment 不執行 provider native 抓取 gate。不可因驗收而自動 start controls、重設首次日期，
也不可將 FinLab／Shioaji／Twelve Data／TAIFEX 或 R2 未完成的 native 驗收描述成部署成功的一部分。

## Runtime 與持久化狀態

每次 Fetcher bundle v5 部署固定安裝八個 consumers：每個 provider 一個 `findb-full-market-<suffix>`
與一個 `findb-historical-<suffix>`。Daily 使用 `findb-fetch-runtime --execution-profile full_market`、
五條 feeds 與 calendar；沒有 Pilot profile 或 enable flag，安裝拓撲不隨設定改變。

| 狀態 | 路徑 |
| --- | --- |
| Staging Full | `/var/lib/findb-full-market/staging/state.sqlite3` |
| Production Full（保留既有路徑） | `/var/lib/findb-full-market/state.sqlite3` |
| 共用帳號 governor | `/var/lib/findb-account/<environment>/governor.sqlite3` |
| Historical request state | 原 provider `/var/lib/findb-*-fetcher/historical`，Twelve Data 為 `/var/lib/findb-fetcher/historical` |

Historical Twelve Data／FinLab 以 Backend request／item lease 為持久狀態；Shioaji 另以 request UUID 命名 SQLite。
驗收應依實際 entrypoint 與 mount 判定使用中的檔案，不能把共用 helper 的未使用通用 state path 當成遺失 checkpoint。

四個現役 `findb-historical-*` workers 是獨立 transactional consumers，沿用既有人工歷史回補請求機制；
所有 acquiring consumers 使用同一帳號 governor mount，保留 credential pins、
quota debt、unknown liability、cooldown 與 prepared delivery。不同環境仍使用各自帳號與儲存。
容器 UID/GID 為 10001，state directories `0700`、SQLite `0600`；provider credentials 不跨 consumer 注入。

部署前 Owner 必須停止所有 controls，等待 desired／observed 都 stopped，取消或等待 historical／maintenance
acquiring work 完成。Idle daemon 仍顯示 running 是正常狀態；helper 會優雅停止並確認 exit 0、非 OOM、無 Docker error。
Provider container 收到 TERM 後有 90 秒完成當前 bounded operation 與回收隔離 child；此上限涵蓋
FactSet 最長 60 秒查詢、額外 20 秒 supervisor deadline 與 process-group reap 緩衝。超過時 Docker 才送 KILL，
helper 會將非零 exit 視為 `graceful_stop` 失敗並觸發交易回復，不會接受或啟用候選版本。
這 90 秒不是完整 historical 日期的排空預算；例如 FactSet 會逐成員交付並等待 normalization，
整日 frozen cohort 可能需要數小時。停止 Full controls 不會停止 historical，也不代表
provider owner 已釋放。部署前須另確認 historical／maintenance work 已結算、所有已接受 runs
均為 terminal、provider owner 無 active attempt；不能只看八個容器 running／健康就重試。
若因 historical 未排空而停止失敗，先確認 transaction aborted、原 container IDs／current 已恢復，
保留原 request、prepared bytes 與 receipts，等待原工作繼續完成。略過 native 驗收不等於授權取消
該 request；取消須另有 Owner 指示。排空後才重跑失敗的 staging CD jobs，不延長或略過停止檢查。
Backend API 以 `exec uvicorn` 接收停止訊號，dispatcher 使用 init 轉送訊號；raw cleanup
收到 TERM 後完成當前 transaction 並中斷每日等待。禁止用 `docker rm -f` 越過停止或 checkpoint 問題。交易保存原 container ID、image、名稱與 running state；
失敗或 INT／TERM／HUP 依反向順序恢復已接受的統一版 runtime。查詢失敗不代表不存在。

## 首次切換與 rollback 下限

首次切換先建立 SSM 設定、Secrets Manager configuration／TLS 及 main-ref IAM trust，驗證四組 schema。
先 staging，後以同 digests promotion production。每個環境都先人工停止並 drain 舊 runtime，保存 RDS snapshot／
可還原點、完整 SQLite＋sidecars、governor、raw marker、舊 current pointer 與 container inspection。
備份要能對應同一停止時點，並記錄 checksum；不得在 SQLite writer 還活動時直接複製單一主檔。

統一版切換時，舊 containers 及 current pointer 在備份與停止證據完備後移出 active installation，
保留狀態目錄；migration 保留 full plans、first dates，原 full controls 不被自動 start。其後 Pilot profile
已退役，見下節。

新 manifest／acceptance 僅接受 schema version 3；Fetcher runtime profile 固定 `unified`。
舊 v1／v2 accepted replay、舊 pilot layout、舊主機路徑及 cutover 開關不再支援。
首次切換失敗時保持停止，以成套備份恢復，不能直接讓舊 image 操作新 schema／checkpoint。
成功後統一版本是 rollback 下限；schema migration 不提供 downgrade，日常 rollback 仍須通過 DB compatibility。

Rollback 入口與 promotion 相同，選擇 `mode=rollback` 和該環境已 accepted 的 exact bundle key。
它不重建 images、不修改 immutable acceptance、不刪 governor 或 prepared bodies。若 migration 不相容，採 forward fix，
或在停止狀態下進行已核對的完整備份復原。

## Universe 上市日的發布順序

Migration `b5d8e2f4a6c1` 新增 `universe_member_listing`（expand-only），只為官方來源有上市日的成員
保存 `listed_date`；`universe_member` 本身不變。Backend schema 以 `extra="forbid"` 驗證 universe 成員，所以帶 `listed_date` 的 Fetcher 送到舊 Backend
會得到 422，TW universe 同步因此失敗。**FinDB 必須先部署到該環境，Fetcher 才能部署。**
Staging 同 SHA 自動部署時，Fetcher CD 會等 FinDB CD 成功；production 兩個 promotion 沒有自動等待，
必須先 promote FinDB 並確認 migration 完成，再 promote Fetcher。Fetcher rollback 不受影響；Backend
rollback 到不含此欄位的版本前，須先 rollback Fetcher。

## Historical Full release cohort 的發布順序（#409、#433）

Twelve Data／FinLab 新建的 historical request 改為每日期凍結 Full release cohort（`universe_snapshot`
`version: 2`, `cohort: full_release`），Fetcher 以 Full adapter 逐成員交付 `market_eod` v1 `backfill`
並以逐成員 EOD evidence（可 `partial`）完成。舊 Backend 不認得這種 snapshot、v1 EOD manifest 與
Twelve Data／FinLab 的 `partial`，新 Fetcher 送到舊 Backend 的 cohort 完成會被拒絕。**Production 必須先
promote FinDB（Backend），確認可建立新 request 後，才 promote Fetcher。** 沒有 migration；Staging 同 SHA
自動部署時 Fetcher CD 會等 FinDB CD 成功。

舊 Fetcher 遇到新 cohort request 時沒有成員清單可用的新路徑，仍會執行舊 executor，因此在 Fetcher
升級前不要為 Twelve Data／FinLab 建立新 historical request。本變更前建立的 request 沒有 cohort，新
Backend 繼續以單一 Source run 驗證、新 Fetcher 繼續以舊 executor 執行。Fetcher rollback 前先確認沒有
running 的 cohort request；Backend rollback 到本變更之前時，須先 rollback Fetcher 並取消未完成的
cohort request。舊 executor、`scheduler_state.py` 與靜態清單的移除見 [backlog](../dev/backlog.md)。

## Pilot profile 退役與 rollback 下限

Backend migration `a7c3e1f9d2b4_retire_pilot_profile`（接在 `e9f4a1c6b3d5` 之後）與 Fetcher bundle v5
（marker `# pilot-retired-v1:`）退役 Pilot execution profile 與 `FULL_MARKET_ENABLED`；daily 只剩 Full market。

Migration 先確認所有 `pilot_*` controls desired／observed 皆 stopped、Pilot provider work 已 terminal、
attempts 已 settled（`calendar_closed` work 的 queued attempts 除外）、owner 未持有 Pilot attempt、Pilot
prepared 皆有 receipt、Pilot plan parts 的 runs 已 terminal；任一未結即以
`Pilot retirement blocked by unsettled <項目>` 整筆拒絕。通過後：

- 刪除 Pilot plans／parts／members／decisions／dataset state、provider work 與 attempts／prepared／receipts、
  綁定 Pilot 的 Shioaji closure／repair rows、Pilot admissions 與 feeds、`pilot_*` scheduler controls，以及
  `PILOT_INCOMPLETE` 等 Pilot plan alerts。
- 保留 Pilot 寫入的 ingestion runs、raw payloads 與 canonical rows；runs 的 `delivery_part_id` 設為 NULL，
  其 `delivery_member_evidence` 刪除。`admin_audit_event` 為 append-only，不改動。
- 只因舊 server default 被標為 pilot 的 legacy alerts 改標 `full_market`；與既有 full_market identity
  重複者刪除。
- CHECK 要求 `full_market_dataset_state`、`daily_delivery_plan`、`daily_execution_decision`、
  `scheduler_control`、`missing_delivery_alert` 的 `execution_profile = 'full_market'`；`provider_work` 允許
  NULL 或 `full_market`。`full_market_environment` 只保留為環境鎖定列，`enabled` 欄位不再使用。

一版相容的收斂（#450）分兩段：

1. 已完成：authorization／scheduler 回應不再回傳 `full_market_enabled`，`ReadinessReport` 不再接受
   該欄位（現行 Fetcher 皆不讀寫）；Fetcher host preflight 只接受已安裝 bundle v5，殘留的
   `findb-pilot-*` consumer 一律阻擋發布。
2. 已完成：2026-11-10 以 `infra/env/aws_configuration.py sync` 清除 staging 與 production 的 FinDB／Fetcher
   SSM 中的 `FULL_MARKET_ENABLED`（staging、production 皆為 version 2→3，可回退 version 2），之後才移除
   `RETIRED_FIELDS` 容忍；現在含此鍵的設定一律拒絕。若回退 SSM 到 version 2，必須同時回退到仍容忍此鍵的版本。
3. 已完成第一步：migration `d7a2c4e6f8b1` 為 `full_market_environment.enabled`（false）與
   `full_market_enrollment.reported_enabled`（true）補 server default；程式不再讀取或指定這兩欄，ORM 只以
   相同常數 default 寫入，讓 migration 測試在舊 revision 以現行 ORM 建立種子時仍有值。
   第二步：ORM 不再映射這兩欄，insert 改由 server default 補值。舊 revision 的 migration 測試以
   `tests/migration_database.py` 的 `add_retired_column_defaults` 補同一 default 後建立種子；schema drift 測試
   暫時允許這兩欄只存在於 DB。第三步：migration `e8b3d5f7a9c2` 刪除兩欄並移除 drift 允許清單；downgrade 以相同
   server default 加回欄位（刪除的值不保留，沒有版本讀取）。production 須依序 promote `d7a2c4e6f8b1`、ORM 不映射兩欄、
   `e8b3d5f7a9c2` 三版。舊 revision 的 migration 測試仍以 `add_retired_column_defaults` 補 default 後建立種子。**production 必須先 promote 含 `d7a2c4e6f8b1` 的版本**，再 promote 刪欄版本；
   不得跳過：舊版仍明確寫入這兩欄，直接升到刪欄版本時，部署切換期間仍在執行的舊版與 rollback 都會寫入已刪除的欄位。

評估結論：`full_market_environment` 保留為 start、plan 與 HK cutover 共用的環境鎖定列，不改為 advisory
lock（列鎖可與其他 row 一起在同一 transaction 中觀察與稽核）。`execution_profile` 只剩 `full_market` 但
保留：它在多張表的 PK／unique 與 Source／Fetcher wire 參數中，expand／contract 移除的成本與風險高於收益，
且仍是新增 profile 時的擴充點。

Fetcher bundle v5 每個 provider 安裝 Full 與 historical 兩個 consumers，四個 providers 共八個；
`release_fetcher_provider.sh` 只接受 `FETCHER_CONSUMER_PROFILE=full_market|historical`，其他值為
`consumer_profile_invalid`。Rollback 下限只接受 v5（v3／v4 含 Pilot consumers，v1／v2 含 Shioaji）；
host preflight 允許從 v3／v4 主機首次切換。Account allocation 的 `account.consumers` 必須剛好是
`{full_market, historical, maintenance}`，由 `inspect_full_market_installation.py` 檢查。

部署腳本不會 stop 或 `rm -f` 任何 `findb-pilot-*` container：仍在執行者以 `pilot_residue_running`、
非正常結束（非 exit 0、OOM 或 Docker error）者以 `runtime_retirement_failed` fail closed。Activation 的
`pilot-residue-v1` 步驟將 `/var/lib/findb-pilot/<environment>`（含 SQLite `-wal`／`-shm`）封存為
`/var/backups/findb-pilot-retirement/<environment>-<UTC 時間>-<pid>/state.tar`，同目錄寫入
`state.tar.sha256` 與逐檔 SHA-256 的 `manifest.json`，並逐檔驗證；之後依擷取的 container ID 以非強制
`docker rm` 移除容器，確認目錄自封存後未變更才刪除。Candidate 模式只驗證不變更；state 為 symlink 或
含非一般檔案時以 `pilot_state_unsafe` 拒絕。

### 切換程序

1. 預檢：Twelve Data 的 staging 與 production 共用同一帳號與 API key，governor 不設跨環境預算，staging
   啟動 Twelve Data 會直接消耗 production 的 credits／分鐘配額；現行用量遠低於上限，不需事先協調（見
   [設定與啟動](full_market.md#設定與啟動)）。Staging 平時
   不跑抓取，一次只啟動一個 provider；仍須確認 `t3.small` Fetcher host 在八個 consumers 常駐、一個
   provider 執行時的可用記憶體（見 [OOM 紀錄](#2026-10-22-staging-historical-finlab-oom-阻擋部署)）。
2. Owner 以 Admin scheduler API 停止所有 `pilot_*` 與 `full_market_*` controls 至 desired／observed
   stopped，排空 historical／maintenance 與 prepared delivery，確認 runs terminal、owner 無 active attempt。
   再以 `docker stop` 手動停止全部 `findb-pilot-*` containers，確認 exit 0、無 OOM、無 Docker error。
3. 保存 RDS snapshot，以及 Fetcher SQLite checkpoints（含 sidecars）與 account governor 的成套備份並記錄
   checksum。
4. 從已安裝主機的 account allocation 檔的 `account.consumers` 移除 `pilot`，使其剛好為
   `{full_market, historical, maintenance}`。
5. Backend-first：migration 成功後才部署 Fetcher bundle v5，由 `pilot-residue-v1` 封存並移除 Pilot 殘留；
   保存輸出的封存路徑與 SHA-256。
6. Staging 唯讀核對 account governor：這是 staging daily 第一次經共用 governor，確認 mount、pins、
   debt、unknown liability 與 cooldown 均保留。
7. Owner 檢查後一次只啟動一個 staging provider，完成首日 Full 驗收（expected 組成、canonical／Serve、
   記憶體與 OOM）後停止，再換下一個。
8. 以相同 digests promotion production，依步驟 2–7 執行；production 不重新建置。

任何 RDS snapshot restore 後（包含本次失敗復原），先執行 [緊急停止](full_market.md#緊急停止)，
再啟動 Fetcher consumers 或 Owner start。

接受後 Backend rollback 下限為 `a7c3e1f9d2b4`，downgrade 直接拒絕，無法還原已刪除的 Pilot rows；
Fetcher 下限為 bundle v5。失敗時保持停止，以成套備份復原或 forward fix。

## DB、credentials 與 network

Migration 與 application 使用不同 DB role。`reconcile_database_privileges.py` 驗證兩個 URL 指向相同 DB，
application role 不具 superuser／CREATEDB／CREATEROLE，授予 public／raw 所需 DML 與 sequence 權限及 default grants；
不授予 schema CREATE，alembic_version 只允許 SELECT。Migration secret 不得進入常駐 containers。

Deployment credential reconciliation 以 SHA-256 建立 queue-health Admin viewer、lookup Serve、static-cache Serve
固定 identity，輪替保留 audit；preflight check-only transaction rollback，migration／初始化階段才 commit。
Fetcher Source credential bridge 只跨 unit 傳送 hashes，不授予 instance role 跨 unit讀取 secrets。
四個 active provider 的 Source scopes 必須涵蓋對應五條 feeds。統一 runtime 不再需要 Fetcher Serve calendar key。

FinDB RDS 不公開且只接受 FinDB security group；Fetcher 不連 FinDB DB。FactSet consumer 另以 TLS 唯讀連接供應商 MySQL RDS，使用獨立 credentials。Source 經 Cloudflare／nginx 還原可信 IP 後執行 allowlist。
nginx 非敏感設定在 `/etc/findb/nginx`，TLS、lookup key 在 `/run/findb-runtime-secrets/nginx`，
systemd 在 Docker 啟動前重建 tmpfs；缺失時 fail closed。Serve API 保持唯讀。

## 驗證與證據

交付前執行 `pnpm check`、`pnpm test`、`tofu fmt -check -recursive infra/tofu`，兩環境 `tofu validate` 與實際 refresh plan。
部署證據至少保留：release／digest、schema version、設定欄位驗證、IAM subject、備份、drain、migration revision、
SSM command 與終態、candidate health、immutable acceptance 與 activation health。
切換的 SQLite 比對以備份建立基準：Full、governor 與每個 historical request 檔案均需存在且完整，
不能只檢查目前存活的檔案。保留原有資料表欄位與完整資料列，包含 prepared bodies、acquisition status／
actual start／login units／connections；quota、pacing 與 cooldown 不得倒退。新建立的 checkpoint
需另核對 profile 身分、實際 mount 與完整性。停止期間不得意外新增 acquisition／work。
DB 另比對 plans、parts、members、first dates、admissions 與 historical requests／items 的既有資料列 fingerprints，
新 profile／calendar snapshot 欄位依 migration 契約單獨驗證。

Native 資料驗收另列每條 feed 的實際結果與未解問題，見 [backlog](../dev/backlog.md)。

## 日曆政策設定切換

Backend `runtime/configuration` 加入四個獨立開關：`TWELVE_DATA_SKIP_NON_TRADING_DAYS`、
`FINLAB_SKIP_NON_TRADING_DAYS`、`SHIOAJI_SKIP_NON_TRADING_DAYS`、`TAIFEX_SKIP_NON_TRADING_DAYS`，
皆使用 `true`／`false` 字串且預設 `true`（略過非交易日）。
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

兩環境 Backend configuration 均有 24 欄、Fetcher 均有 12 欄；五個 provider skip 開關中
FactSet為`true`，其餘四個為`false`。
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
Runtime 包含 provider-work-v1 protocol。Staging 的既有啟用紀錄見下方 FactSet 章節；
production 的 Owner attestation、enforcement、備份與 release 證據見
[production 維運](production.md#provider-work-切換與舊流程退役發布2026-10-29)。
`infra/env/deployment.schema.json`、兩環境 deployment examples、Compose 與 Fetcher `.env.example`
均使用 `PROVIDER_WORK_REQUIRED=true`；缺省 true，明確 false 會拒絕啟動／部署。
Backend／Fetcher 的環境解析接受一般 truthy 布林寫法（例如 `True`、`1`、`yes`），
但 deployment manifest／runtime configuration 的正式值維持字面 `true`。
空值不視同缺省，會報布林格式錯誤；明確 false 的訊息則指出協定不得停用。
Source outcome 的 report-only continuation 需要 Backend 與 Fetcher 都使用本版實作；
回退到不認得 continuation 的版本前，須先確認 `source_error_pending` 已補報排空，
不能以清除 SQLite reason、帳號 cooldown 或重設 queue 來處理待回報工作。
新版本不再提供舊 acquiring／無 fence acceptance fallback。程式碼變更本身不會同步遠端設定，
實際環境的部署與設定值須依上述驗收紀錄核對。
Backend 與全部 Fetcher consumers 必須一起切換。完整邊界見
[provider work 架構](../architecture/provider_work.md)。

新版本的 rollback 仍須保留 provider-work enforcement。若要退回不支援此協定的舊 binary，
須先停止、排空並 reconcile 新版產生的全部工作與交付，保存原 DB／SQLite／raw／receipts／
budgets／cooldown；不能將新版本設為 false 或回復較舊用量來恢復舊流程。未釐清原 prepared／
receipts 與子程序狀態時停止切換，不能清檔或重新初始化。Production 的現場待辦見
[production native 與回復驗收](../dev/production-foundation-cutover-plan.md)。

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
   對每個 provider 執行新 CLI 的 `--initialize-state`，只建立／驗證原 profile state 與 shared lock inode，
   不取得或解除 provider flock，也不代表已排空。一般部署補建另一 profile checkpoint 時，
   同 provider 的其他 profile 可仍持有工作鎖；本節的首次 cutover 仍要求全部 consumers 停止。
   不得重建遺失的既有 checkpoint。保留單環境唯一 Fetcher host 的 trusted deployment inventory。
4. 保存完整 consumer `docker inspect` JSON（不可將含 secrets 的原始內容貼進 audit／chat），以及只包含
   該環境 Fetcher host IDs 的可信 inventory JSON。以 host local 的 `infra/deploy/provider_work_preflight.py`
   驗證單 host、bundle v3 的全部 12 個 consumers、protocol flag／image label、graceful stopped state、同一 writable
   bind mount，並實際嘗試取得 FactSet、FinLab、TAIFEX、Twelve Data 四個 provider flock。任一 orphan／未知 consumer／不同 inode 都拒絕。

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
   保留各 profile desired state、first dates 與 frozen plan。切換後 historical 不要求停止 Full controls。
   若原環境已安裝 accepted protocol-v1 images，重建 enforced consumers 後尚未啟動的真實狀態是
   `created`；它不能代替原 workers graceful stop 的證據，也不能直接通過 installed-host guard。
   先保留原十五個 workers 的 exit 0／完整 inspect／備份，完成 Owner attestation，再依 Backend-first
   順序部署 Backend。只有 controls 已停止、historical requests terminal、無 maintenance／rerun 或
   其他可 acquisition／待 drain 工作時，才啟動相同 exact accepted images 的 enforced consumers，
   驗證真實 running／零重啟與完整 installed-host guard，接著發布新 Fetcher。不得修改 Docker State、
   換 entrypoint 假造退出、跳過 guard，或啟動 false consumers。
7. 驗證同 provider daily → historical → daily 的完整 canonical proof、不同 provider 並行、heartbeat-loss
   恢復原 owner、cancel／prepared drain，以及 Dashboard queue projection，才宣告該環境完成 cutover。

Rollback 也必須先停止 acquisition、證明 child reaped 並 drain accepted runs。不能在新 owner／prepared work
尚未排空時啟動舊 image，不能因 TTL 到期直接清 slot；expand tables／evidence 不 downgrade。

Provider-work 尚未啟用的 governor upgrade 另以 additive `full_account_cooldown` 保存 floor reason。
原 full_pacing／quota／intents／audits 不刪、不重設；舊資料只有 deadline 時保守作 legacy cooldown，
可能多一個安全 tail wait。明確正常 request pacing 維持原間距，不拆 SDK bootstrap。Upgrade/reopen
只延長 debt；rollback 仍須依上述完整停止／排空／成套checkpoint備份程序，不可倒退 usage／cooldown
或混用未通過此 dispatch gate 的 consumer。Runtime 部署成功不代表 provider-work queue 已啟用。

## Provider catalog PR2 發布邊界

PR2 使用最新 schema-v4 單次部署交易；不新增部署 job、第二次啟動、舊 candidate／activation
腳本或 host override。沿用 backend-first orchestration、immutable decision、committed journal／receipt、
獨立 watchdog 與 recovery gate。Backend metadata 與 provider 型別已由既有 Docker `COPY app`
打包，不依賴 release bundle 中另放 catalog 或讀取主機工作目錄。沒有新 migration。

PR1＋2 保留舊 Fetcher 相容性；PR4 起新 bundle 使用 bootstrap 與生成 inventory。
Staging 驗收應核對最新 committed receipt、current manifest、實際 images 與 metadata digest，
以四個 Source identities 讀取 execution-config，驗證各自 scope、環境、profile、版本、時區、
deadline 與 no-store。控制狀態不因讀取設定而改變。

Production 發布分組為 PR1＋PR2、PR3＋PR4、PR5，保留每階段 staging 的 immutable evidence：
先發布 PR1＋2 Backend API，接著 PR3＋PR4 Fetcher／Infra，最後 PR5 preflight／migration／Backend／Dashboard。
每階段 promotion 已驗收的相同 digests，不能只用最終版本跳過中間相容階段。沿用已完成的
production transactions prefix 保護與 schema-v4 發布基準（見上文首輪實測）；本 PR 不執行 production apply 或發布。
PR4 上線後，Backend 不得單獨回退至沒有 execution-config API 的版本。

## Provider catalog PR3 發布邊界

PR3 只解除 historical 執行與 planner 的本機排程依賴，不新增 DB migration、排程 API 呼叫或控制面權限。
`execution_descriptors.v1.json` 已由三個 Dockerfiles 的 `COPY fetcher/src` 打包，wheel 同樣攜帶此檔。
Historical `--check` 驗證連線設定及 package metadata 的版本／digest／provider scope，不會 claim、
建立 state 或抓取 provider。PR4 已解除 daily 與部署工具的剩餘依賴，舊排程檔只保留在測試 fixture。

沿用目前 schema-v4 單次交易、三 images／四 providers／十二 consumers inventory、
whole-work owner lock、checkpoint identity、immutable decision 與 committed receipt；不新增部署入口
或第二次啟動。現有 change policy 會選取 Fetcher rollout，Backend CI 仍需驗證跨套件 runtime tests。
合併後可依既有 staging CD 驗證 PR3；production 等 PR4 整合驗收後，以 PR3＋4 同一組 accepted
Fetcher／Infra bundle promotion，先停 controls、排空既有工作並保留 checkpoint，再依原流程切換。
本次實作不 dispatch production，也不自動恢復或啟動 controls。

PR3 驗證包含舊排程路徑失效、非排程預設與舊 manifest 相同、reviewed universe 與 frozen members 保留、
原 job key／attempt／prepared bytes 重啟復用、Backend preview 拒絕時不建立 request，以及既有
Source claim→acquire→prepare→receipt→complete、cancel／expiry／ACK-loss 測試。
PR3 的唯讀驗收不替代 PR3＋4 的 daily 動態排程與 historical 完整鏈路驗收。

## Provider catalog PR4 發布邊界

新 Fetcher bundle 使用 `fetcher_bundle_version=4`，部署交易仍為 schema 4／single-pass-v1。
三環境 `bootstrap.*.v1.json` 僅含 version、deployment_target、poll_seconds；bundle 加入生成
`provider_inventory.v1.json`，不包含舊 full-market schedule。三 images／四 providers／十二 consumers
由 catalog 投影決定，host eligibility、recovery gate 與 Backend owner-lock 使用相同 inventory。
CD 在 materialize release selection 階段只接受 bundle v3／v4；拒絕 v1／v2 與未知版本，
不可將 Shioaji 退役下限誤寫成僅接受 v3。此階段失敗尚未執行 SSM preflight／apply，沒有主機切換
或需要 rollback 的新交易；修正後須由含修正的 main revision 重新發布。
原已接受 bundle 3 的 allowlist、manifest digest 與舊 command binding 仍可驗證，允許升級及原交易回復。
Installed receipt 依 bundle 格式驗證設定：v4 使用不含 desired_state 的 bootstrap，v3 保留舊設定檢查；
兩者都保留執行中 image、config digest、環境、Source 與 account allocation 的綁定。
未知／重複／缺少 consumer 仍阻擋發布，不把它當作空主機。
主機上的已停止備份容器也屬於完整 Docker inventory。若仍有 provider-work 初次切換保留的
`provider-work-backup-*`，preflight 回報 `consumer_inventory_mismatch` 並停止；不因名稱為 backup
而自動忽略，也不在 CD 內自動刪除。先核對現役十二個 consumers 的 accepted identity，逐一確認
額外容器來源與停止狀態，將 rootfs export、inspect、diff、mount inode 與 SHA-256 manifest 保存於
root-only 封存目錄，經 Owner 核准後才按固定 container ID 移除舊容器。保留 images、所有 volumes、
checkpoint／cache 與 bind mount，禁止 `--force`／`--volumes`；清理後重驗原現役 ID、mount inode、
host guard，再重試 staging CD。此檢查不代表需要回退已成功部署的 Backend。


沿用 main-ref OIDC、backend-first、不可變 promotion decision、committed journal／receipt、獨立
watchdog 與 activation failure 完整回復。PR4 不新增 migration、不改 controls，也不取代現有人工
Owner stop／排空／checkpoint 保全流程。新 bundle 不自動啟用原本停止的 provider。
先在 staging 驗證 bootstrap、daily API 輪替／失敗、prepared recovery、四個 historical consumers，
再按 PR3＋4 組別 promotion 相同 accepted digests；本次實作不部署 production。

PR4 Fetcher 上線後，Backend 最低版本必須提供 schema 1 execution-config 與
`PROVIDER_WORK_FROZEN_FEED` 相容回應（本 PR4 Backend）。因此本組沿用 backend-first 發布，
不可只更新 Fetcher 或單獨將 Backend 回退至 PR2。日後調整同 schema 的
排程時間只需發布 Backend；新增 adapter／contract 能力或超出現有 schema 才需協調 Fetcher 發布。
PR5 的 DB schedule／deadline、monitor 與 Dashboard 切換見下方發布與遷移規則。

## Provider catalog PR3 staging 驗收

2026-11-10（台北時間）驗收已部署 [#430](https://github.com/FPI-TW/findb/pull/430)，合併 commit
`6574b2839ac71d9b698fe07c96feae34afa08e2b`。FinDB CD
[42721900884](https://github.com/FPI-TW/findb/actions/runs/42721900884) 與 Fetcher CD
[42721900816](https://github.com/FPI-TW/findb/actions/runs/42721900816) 均成功，沿用 Required CI
[42660861164](https://github.com/FPI-TW/findb/actions/runs/42660861164) 證據。

- 兩台 current manifest、S3 immutable decision／accepted record 與 host journal／receipt identity 一致，
  schema 4、journal committed、receipt healthy／accepted。Backend bundle SHA-256
  `3e4daaac82b37b5dde2ec3d6c12aecf7ea1c269d4eebdfcc37d8d35db758baa0`；Fetcher bundle
  `f8e5c4cdbca2df228398a4a404ca5417b202d19a9abd1a55481a461c081dcd9d`。
- Backend 六個服務與 Fetcher 十二 consumers 正常，images 符合 manifest，無 OOM／restart。
- 四個 historical 容器在 subprocess 設定失效的舊 schedule/config 路徑後，`--check` 全部通過；
  六個 historical runtime 模組 SHA-256 與合併版一致。FinLab historical 仍只開 equity，未擴張 ETF。
- 四組 Source credentials × Pilot／Full 的八次 execution-config 回應均為完整授權 scope，
  environment／profile／schema／digest／no-store 正確且無 slot。每 provider 七組負向查詢均拒絕。
- Admin 兩個 profiles 的五條 feed schedule 與 catalog 一致；Shioaji Pilot 各 11 個歷史 plans
  仍可唯讀查詢且 deadline 與 DB 一致，Full 無 plans。
- Admin GET 前後 DB 14 個 controls（全 stopped）、14 個 first dates、70 個 frozen plans 與狀態
  完全相同；無未撤銷 Shioaji credential，migration 為 `d8e3f0b5a2c4`。

唯讀 SSM 證據：Backend `e0a4e671-10e3-4350-b76d-958c68b7bbad`；Fetcher／Source／historical
`5315c922-141c-4a78-8dd2-36b3ba5dc768`；Admin／DB `b5b1a716-c373-4b1f-8ae5-89bdd871de52`。
本次未新增 backfill request、未抓取 provider、未變更 controls，亦未部署 production。
因此通過的是 release／bootstrap／API／frozen 資料相容性；live historical 全鏈路與 PR4 動態排程
留待 PR3＋4 staging 整合驗收，不把本次唯讀結果當作完整 native acceptance。

## Provider catalog PR3＋4 staging 整合驗收

2026-11-10（台北時間）驗收 PR3 [#430](https://github.com/FPI-TW/findb/pull/430)、PR4
[#431](https://github.com/FPI-TW/findb/pull/431) 與部署修正 #432／#436。部署 commit 為
`d86b633aae3ac83cdb0ab1f789057d16d28b1f2c`；[FinDB CD 42737364615](https://github.com/FPI-TW/findb/actions/runs/42737364615)
及 [Fetcher CD 42737364735](https://github.com/FPI-TW/findb/actions/runs/42737364735) 均成功。
Fetcher bundle SHA-256 為 `3a69d56717972b5ff38b9147c7450e2bf240c0853453ba97bfc9f36e773b8b3c`，
schema-v4 journal committed、receipt healthy，十二個現役 consumers 符合 manifest／host guard，
無 OOM／restart。三個 image digests：

| Image | SHA-256 |
| --- | --- |
| Twelve Data／TAIFEX | `74b6cbe56268beae369f1cec92688a5ecb66f8011adab613adc6bd89274c2f87` |
| FinLab | `a9c796f906e23f3f58d0ec09f153f2e7a903701ee393415de2e587fe9549ebb7` |
| FactSet | `195727b08e554850860cf38b947439fe66fdf301d1ac5385ab8414bdaef4e1eb` |

### 已完成項目

- 四個 providers × Pilot／Full 共八次正式 Source execution-config 查詢均通過部署 image 的
  resolver；scope、environment、profile、schema、digest 與 `Cache-Control: no-store` 正確。
  Daily／historical `--check` 通過，舊 full-market／daily schedule 檔不在 image，指定失效舊路徑
  不構成 fallback；FinLab historical 仍只包含 equity。
- 相同 image digests 在無網路、無 secrets、無現役 state mounts 的隔離容器內，完成 883 項
  非信號測試（FactSet 291、FinLab 273、Twelve Data／TAIFEX 319）。涵蓋完整快照換版、下一輪
  生效、API 失敗清除舊快照、prepared recovery 不要求新排程、DST／跨年邊界、historical
  metadata／planner、owner fence、raw-first、ACK-loss、lease／retry／cutoff 與恢復案例。
  Provider／Source 故障由測試 doubles 注入，未故意中斷 staging API 或更改線上排程。
- 本機跨架構容器的少數 process signal 案例曾逾越三秒上限；將原測試函式在 staging 原生
  amd64、相同三個 image 的隔離容器執行後，48 項 SIGTERM／SIGINT 測試全部通過，包含
  Source outage wait、historical idle／running／failure／startup 的排空與退出。
  原生 probe 無 credentials／網路／現役 mounts，執行後移除自身容器。
- 經已登入的 staging Owner Dashboard preview／create 建立 `2026-11-06` 單日請求。
  全程 daily controls 維持 stopped；未更改 universe、首次日期、日曆、配額或權限。
  三個完成請求均在第一個 lease／attempt 結算，prepared → receipt → raw payload 雜湊一致；
  canonical 與受既有 Serve credential 保護的查詢皆有本次資料。

| Provider | Request ID | Native 驗收 |
| --- | --- | --- |
| Twelve Data | `01a1c333-6218-7098-abf1-968b2dceed30` | completed；staging 原有 3 成員、3 receipts、3 筆 canonical／Serve；3 個 R2 raw objects 雜湊正確。 |
| FinLab | `01a1c333-f07e-733f-9508-deab69419f83` | completed；staging 原有 2 成員、1 receipt、2 筆 canonical／Serve；5 份 native response checkpoint 與 R2 aggregate 雜湊正確。 |
| TAIFEX | `01a1c334-69bb-70e0-9610-f0c61672946c` | completed；TX／MTX／TMF／TE／TF，62 個契約／時段 observations、53 筆 canonical／Serve；五份 CP950 CSV 及整日 manifest 的 R2 bytes 均符合 SHA-256。 |
| FactSet | `01a1c335-001b-72a3-bf78-97f6243ab083` | Owner 指示本次跳過終態驗收；已建立請求保留，未取消或改寫。詳見下節。 |

Historical 成功發生在當地 daily 觸發時間之前且 controls 全部停止，未新增 daily running 或
觸發時間 gate。這是 staging 原有 universe 的實測，不代表已驗收 production 的 101／50 檔清單。
Production planner 以隔離測試驗證 preview／scope／估算與回補語意，未指向 production 建立工作。
既有 14 個 controls 的狀態／revision／retired 值、14 個 first dates，以及 70 個 frozen plans 的
identity／deadline／member hash／expected count，驗收前後雜湊完全相同。

### FactSet 保留紀錄與驗收限制

Owner 指示先跳過 FactSet，故不宣稱四個 native historical runners 已全部完成驗收。
PR3＋4 驗收時 DB 觀測請求仍為 running，凍結當時 HKEX 2,817 個成員；原生價格回應 2,648 列
已保存 R2，SHA-256 為 `e5a1aef3190c656c08257166fc471eb49299ec66bd277883ba2b0f0e3fc417fe`。
已觀測 295 個 prepared／receipts／raw payload，兩段 payload hash 均相符；其中 285 筆已有
canonical 寫入，Serve 第一頁 100 筆皆來自本次抓取。此數字是執行中快照，不能當成最終完整性。

不縮減 frozen cohort、不把缺列當 no_data，也不以目前成功筆數推定 completed。後續如恢復
FactSet 驗收，先按上述 request ID 唯讀查詢終態，再核對全部 receipts、DQ、member evidence、
owner 釋放及 partial／completed 結算；不要另建重複請求或重抓已保存的 native bytes。
本次不部署 production，不把排除 FactSet 的結果視為其 production promotion 核准。
當時部署版本仍顯示 Slot；PR5 切換市場分組後，slot 契約／DB 欄位仍維持相容。

2026-11-10 PR5 staging CD 復原時，原請求於台北時間 12:02:00 自然結算為 `partial`，
request／item／work 終態一致，2,648 份 prepared／receipts 對應的 runs 全部 terminal
（2,573 completed、75 completed_with_errors），provider owner 已釋放。未取消、重建請求或
重抓 native bytes。這次只確認安全部署所需的排空條件，不取代尚未執行的 FactSet 完整 native
驗收；唯讀 SSM 證據為 `ad8b81cb-b1af-4983-8a0a-6f66ac063496`。

### 可追溯證據與本機驗證

| 驗收 | SSM command ID |
| --- | --- |
| 最新 release、host guard、八組 Source 快照與 bootstrap | `4c114132-995e-4d53-84ed-0681f77a133b` |
| DB 操作前基線 | `b77313bc-0e92-4440-bb4c-45133852d0b6` |
| Native request／work／receipts／canonical 與 frozen 資料比對 | `9baa3885-2bb3-44fd-91fb-bc1b9450bcd3` |
| Journal prepared／response 及 R2 raw 雜湊 | `bb8ec7d2-25fa-4528-b0f3-bb5b1d8447c0` |
| TAIFEX 五份報表與整日 manifest | `4c8ab364-e880-4a6a-bef2-d5ab9b261319` |
| 三個 image 的原生 process signal 測試 | `a5e64d34-4264-4189-af50-a4a948828954` |
| 已認證 Serve 四市場查詢 | `e9f89dec-ebb7-4460-8a54-d9a29a79965e` |
| 驗收後仍為十二個現役 consumers、無測試容器殘留 | `d0ac4740-de10-46d1-bdbb-be5160d5d5ab` |

全 repo `pnpm test` 完成：Backend 2,259 passed／3 skipped、migration 31 passed、Infra 986 passed／
2 skipped／289 subtests、Fetcher 1,771 passed、Dashboard unit 203、browser 10、e2e 16 passed；
contracts／generated artifacts checks 通過。`pnpm check` 的 format／lint／type checks 通過。

## Provider catalog production 驗收

2026-11-10（台北時間）完成 PR1＋2 production 唯讀驗收。
[FinDB Production CD](https://github.com/FPI-TW/findb/actions/runs/42656213012) 已完成 promotion，
主機 commit 為 `077ee02b5d2c26b42927ad66e236bdeb3965ddc7`。Backend／Dashboard digests 與
PR2 staging accepted manifest 相同；Fetcher 保留 `c1c175b8a7496ff666472e3ee83ffcc9483f3756`，
本次未重部署 Fetcher，亦未因驗收新增任何 production dispatch。

- Backend 六個 application containers、Fetcher 十二個 consumers 皆 running、零重啟、無 OOM；
  有 healthcheck 的四個 Backend containers 均 healthy。兩台主機 current manifest、實際 images、
  schema-v4 committed transaction／receipt 與 S3 immutable decision／acceptance 的發布身份一致。
- Backend metadata SHA-256 為 `e6a89f5b6f509ef73098c9109e52d11485bfc30a2bcc846ed77f2a6471c7eb44`。
  四個 provider 使用原有 Source credentials 驗證 Pilot／Full 共八組 execution-config，全部 HTTP 200；
  identity、production／profile／schema、完整 catalog 內容、digest、no-store 與無 slot 均正確。
  每個 provider 的五種錯誤 query 皆 422，缺少／無效 key 分別 401／403。
- DB-backed viewer 查詢 Admin schedulers／freshness，Pilot／Full 各五條 feed schedule 皆與 catalog 一致。
  #423 的退休 Shioaji Full minute plans 兩個 datasets 各 18 筆，全部 HTTP 200，deadline 逐筆符合
  frozen DB 值；Pilot 查詢回傳空集合。公開 health、Dashboard lookup、Serve US EOD 均 HTTP 200。
- Admin GET 前後 13 個 controls 的狀態／revision／retired 值、7 筆 first dates、141 筆 plans 的
  identity／deadline／membership hash／expected count 及 work／historical／normalization 狀態計數一致。
  DB revision 保持 `d8e3f0b5a2c4`，未撤銷 Shioaji credentials 為零；歷史終態未重寫。
- Production Twelve Data／TAIFEX Full 的 desired state 原為 running，當時 observed state 為 stopped；
  runtime 在 idle／每輪完成後會回報 stopped，只有正在執行一輪時回報 running。其餘 controls 維持 stopped。
  初次 probe 誤沿用 staging「全部 stopped」的斷言而中止；改為記錄實際狀態並比對 GET 前後，完整重驗通過。
  未啟停 controls，也未以此次 GET 驗收宣稱 daily 或 historical 新工作完成。

| 證據 | FinDB | Fetcher |
| --- | --- | --- |
| Production accepted bundle SHA-256 | `19ed28e981b3335435ee97c9703823467e14d350e7ed7484101c049aaa91157a` | `1cac68892f4e3b9263579fc237fd1ce0825be1927dcc04208557ff7bd3140880` |
| Committed transaction suffix | `42656213012-1` | `42639305431-1` |
| Receipt 原始部署 SSM | `67e504ba-1d88-47f6-a263-bb82632d181d` | `a0be3b53-0030-4d16-87f7-81352a258f99` |
| 本次 runtime／Source 驗收 SSM | `51a4f20e-caf3-44c8-aa31-2a436be341b6` | `65833766-bef0-4ffd-bbc5-83f2e6980400` |

Transaction ID 為 `<bundle SHA-256>-<suffix>`。Admin／DB 最終驗收 SSM 為
`5bb58896-32d3-4628-aced-83896dd9e637`。此次只驗收已發布 PR1＋2 的相容性；PR3＋4 與 PR5
尚未 production promotion，不取代後續 native 抓取、歷史回補完整鏈路、動態排程與 DB／UI 驗收。

## Provider catalog PR2 staging 驗收

2026-11-10 00:27（台北時間）完成 [#428](https://github.com/FPI-TW/findb/pull/428) 的 staging
唯讀驗收，Backend current commit 為 `077ee02b5d2c26b42927ad66e236bdeb3965ddc7`。
[FinDB CD](https://github.com/FPI-TW/findb/actions/runs/42651731380) 成功；
[Fetcher CD](https://github.com/FPI-TW/findb/actions/runs/42651731410) 依 change policy 跳過 publish／deploy，
Fetcher 維持已接受的 `c1c175b8a7496ff666472e3ee83ffcc9483f3756`。本次未部署 production。

- 兩台主機均為 schema-v4：current manifest、實際 image digests、committed journal／healthy receipt
  與 S3 immutable transaction decision／accepted record 的發布身份一致。Backend 六個 application
  containers、Fetcher 十二個 consumers 均 running、零重啟、無 OOM；設有 healthcheck 的四個
  Backend containers 均 healthy。未使用舊 accepted label 取代 transaction／container identity 驗證。
- Backend image 的 metadata SHA-256 為
  `e6a89f5b6f509ef73098c9109e52d11485bfc30a2bcc846ed77f2a6471c7eb44`，與合併版本一致。
  四個 Pilot 容器以原有 Source credentials 經正式 HTTPS endpoint 讀取 Pilot／Full 設定，八組皆
  HTTP 200；四 providers／五 feeds 的完整內容與 catalog 相同，scope、staging 身份、schema 1、
  profile、digest、時區、date lag、deadline、bounded monitor、retry window、`no-store` 均通過。
  Twelve Data 只含 US，FactSet 只含 HK，FinLab 含 TW equity／ETF，TAIFEX 含 TW futures；回應無 slot。
- 每個 provider 另驗證七種拒絕情境：缺少 profile、未知 profile、schema 2、額外 provider、額外
  environment 參數皆 HTTP 422；缺少 key 為 401，無效 key 為 403。未更改既有 credential scope，
  NULL／空／重複／過寬 scope 等資料異常案例沿用 PR2 自動化測試，不宣稱已於 staging 注入驗證。
- 以既有 DB-backed viewer credential 查詢 Admin schedulers／market-freshness；Pilot、Full 各五條
  feed 的 schedule 與 Source 使用的 catalog 描述一致，保留逐 feed UTC `next_scheduled_at`。
  [#423](https://github.com/FPI-TW/findb/pull/423) 的退休 provider 相容性也通過：兩個 TW minute dataset
  各回傳 11 筆 Shioaji Pilot plans，HTTP 200，deadline 與 DB frozen 值逐筆相同；Full 查詢正常回傳空集合。
- Admin GET 前後以 read-only DB transaction 比對：14 個 controls 的 desired／observed state 均 stopped，
  revision／retired 狀態、14 筆 first dates、70 筆 plans 的 identity／deadline／membership hash／expected
  count 及 work／historical／normalization 狀態計數均未改變。Migration 維持 `d8e3f0b5a2c4`，未撤銷
  Shioaji credentials 為零。既有 failed／partial 終態保留；不將認證使用紀錄或 runtime heartbeat 視為凍結資料。
- 公開 health、Dashboard lookup redirect 後及沿既有 Referer 規則的 Serve US EOD 查詢均 HTTP 200，
  Serve 回傳一筆 canonical 資料。探測腳本曾因 Backend image 未包含 `httpx`、查詢欄位／分頁欄位名稱
  誤用而失敗；改用標準庫並修正腳本後完整重驗通過，未修改應用、image、DB 或部署設定。

| 證據 | FinDB | Fetcher |
| --- | --- | --- |
| Accepted bundle SHA-256 | `2de016f2d0c05a50aef30c92a1e9004aeb97add2f789cf243f25c578b462d31c` | `7463ebd12ade03c810d1d48ff966bedbc61b6afa72cc9542e119f3b17a2c6603` |
| Committed transaction suffix | `42651731380-1` | `42630986350-1` |
| Receipt 原始部署 SSM | `4bf32a5f-9087-4e12-8ddd-63dc2e32c42c` | `8efc31cb-9790-427e-9ec9-ec41e54f63c9` |
| 本次 runtime／Source 驗收 SSM | `9127f61e-c778-4898-9b84-0bfe7d7c7786` | `24c8f814-8d37-49bb-8915-e47c9ccba768` |

Transaction ID 為 `<bundle SHA-256>-<suffix>`。Admin／DB 完整通過的 SSM 為
`a2f31c30-658e-49dc-9fd6-bddadd6041f0`。本次只驗收 PR2 發布與相容性，未啟動 controls、建立或
執行 daily／historical 工作、呼叫外部 provider 抓取。Historical 完整鏈路、動態快照換版／故障恢復、
DB schedule／deadline 遷移與 Dashboard market 分組仍屬 PR3～5；此次查詢成功不替代後續驗收。

## Provider catalog PR1 staging 驗收

2026-11-09（台北時間）[#416](https://github.com/FPI-TW/findb/pull/416) 合併至
`266e3f2cfde77cb74bc6086d4a2197851cc52301`，
[FinDB CD](https://github.com/FPI-TW/findb/actions/runs/42585169496) 與
[Fetcher CD](https://github.com/FPI-TW/findb/actions/runs/42585169341) 均成功。
17:05 前完成下列 staging 唯讀驗收；本次未 promotion production。

- 兩台主機 current manifest 均為上述 commit、staging、schema-v3，與 S3 immutable accepted record
  的 commit、bundle SHA-256、五個 image digests 逐一一致。
- Backend 六個 application containers（serve、ingest、worker、dispatcher、raw-cleanup、Dashboard）
  均 running、零重啟、無 OOM，images 符合 manifest；設有 healthcheck 的四個 containers 均 healthy。
  十二個 Fetcher consumers 均 accepted、running、零重啟、無 OOM，image 與環境身份正確。
- Backend image 的 metadata 為四 providers／五 feeds，Source scopes 與現行 runtime 相同。
  檔案 SHA-256 為 `e6a89f5b6f509ef73098c9109e52d11485bfc30a2bcc846ed77f2a6471c7eb44`。
  四個 provider 的 Pilot 容器內 descriptor 同為
  `fea4d093c2d718a2476d1f5deeccd64ad581e72a738719ee4591723928f17133`，與合併版本一致；
  不含 schedule、timezone、date lag、deadline 或 publication retry window 欄位。
- 四個 provider 使用容器原有 Source credential、`httpx` 與正式 HTTPS endpoint 讀取 ingress
  contract，全部 HTTP 200。首次以 `urllib` 探測得到 403，改用 runtime 使用的 client 重驗通過；
  未調整 API key、allowlist 或 Cloudflare 規則，也未將首次失敗列為通過。
- 公開 `/health` HTTP 200；Dashboard lookup 正常 redirect 後 HTTP 200；依既有 Referer 規則
  經 Serve API 讀取 US EOD，HTTP 200 且回傳一筆 canonical 資料。
- DB 使用 read-only transaction 查核：revision 維持 `d8e3f0b5a2c4`；所有 controls 均 stopped，
  Shioaji controls 維持 retired，未撤銷 Shioaji credentials 為零。Normalization 與 historical
  request 皆無非終態項目；既有 failed／partial 終態未改寫或宣稱修復。

| 證據 | FinDB | Fetcher |
| --- | --- | --- |
| Accepted bundle SHA-256 | `12b0cdddc064da643c89fefbfaab1b285d793e04bd90786bf405fac12c351783` | `2bd6c67302c98df5dace4c7455470572e886da58c51dac8f77e73be057615ded` |
| Candidate acceptance SSM | `687d48ec-b6b8-4b14-b89c-b8938e556940` | `b1c6770b-5a4d-422b-ac06-e811a35b1686` |
| 本次唯讀驗收 SSM | `49bf38f9-e082-48fd-937b-c307204f0556` | `aa119869-ac99-4873-83f9-5f97fe994bf3` |

PR1 只驗收 catalog 產物發布及既有服務相容性；未啟動 controls、建立 daily／historical 工作、
呼叫 provider 抓取或改寫 DB。未重跑 native 抓取、historical 完整鏈路與逐表保留性比對，
也未宣稱完成 execution-config API、動態排程、market 分組或 slot 退役。
後續範圍見 [provider metadata](../architecture/provider_metadata.md) 與 [backlog](../dev/backlog.md#provider-metadata-與排程統一)。

## Shioaji 退役的 rollback 下限

Fetcher bundle v3（marker `# shioaji-retired-v1:`）包含三個 images、四個 providers、十二個
consumers。兩環境已接受 v3；host guard 不再支援 v1／v2 升級過渡，發現任何 Shioaji 容器
（包含 `-previous` 等殘留）即停止。部署腳本不再自動停止、改名或刪除 Shioaji consumers。
保留的舊容器名稱僅用來拒絕殘留；release manifest 的舊格式讀取仍供 accepted evidence 稽核。

Migration `d8e3f0b5a2c4` 接在 `c7d2e9a4f1b3` 之後，只撤除 acquisition：

- 移除 registry `allowed_sources` 及 delivery expectation 中的 Shioaji；沒有其他來源時關閉
  schedule／missing-delivery。保留其他來源、`source_precedence`、first dates 與 frozen config。
- 撤銷所有尚未撤銷的 Shioaji Source credentials，保留原 hash、scope、歷史使用與輪替紀錄。
  Create／rotate／credential reconciliation 都不再供應 Shioaji。
- 將 Shioaji controls 標為 retired、增加 revision，保留 scheduler-dataset 關聯；關閉既有 open
  missing-delivery alerts。Admin repair inventory／preview 仍可讀，retired control 不可核准重開 work。
- Backend／Fetcher catalog 與 active Source scope 均為五條 feeds。兩條 minute registry 仍
  `is_active=true`，`market_minute.v1`、normalizer、canonical／Admin／Serve／archive 查詢仍保留。
  新版 seed 不會把 operator override 中的 Shioaji expected sources 加回去。

部署前必須確認 Shioaji controls desired／observed 都 stopped、owner 無 active attempt、work 已
terminal、attempt 已 settled、prepared 均有 receipt、historical request 與 ingestion run 已 terminal。
Migration 對任一未結項整筆拒絕；唯一例外是 `calendar_closed` work 的 queued attempts，這些休市
歷史證據原樣保留，不重新入列、不標成成功。原始 raw 繼續遵守既有 retention TTL。

依 staging → production 部署 Backend，記錄並比對 canonical minute、requests／items、runs、plans、
work／attempt／owner、prepared／receipt、repair 與 `tw_minute_*` 的筆數及不可變欄位指紋。
驗收包含舊 Source key 失效、新 key／rotate 拒絕、Shioaji 不在新回補 scopes、歷史 Admin 頁與
Serve minute pagination／archive 仍可查。Staging 的兩筆休市 queued attempts 不應因退役消失。
部署完成後執行 seed／credential reconciliation 再驗證不復活。

接受此 migration 後 Backend rollback 下限為 `d8e3f0b5a2c4`；downgrade 不會復活 credentials、
scope 或 controls。不可 replay 舊 Backend／Fetcher 重新抓取；修復採 forward fix。替代分 K provider
需另建 scope、credentials 與 universe，舊 receipts／frozen plans 不可移作新 provider 成功證據。

### AWS 資產與憑證收尾

退役 IaC 將兩環境 Shioaji Source／provider secret metadata、ECR 標為 `RuntimeStatus=retired`，
從 instance secret-read／decrypt scope 與 publisher／runtime image scope 移除；保留 `prevent_destroy`、
`force_delete=false` 及原有 recovery window。原位停用 Shioaji CloudWatch alarm 通知並標記 retired；移除 collector active feeds／
mount expectations，以及 `SHIOAJI_SIMULATION`、
`SHIOAJI_SKIP_NON_TRADING_DAYS` runtime catalog／環境範本設定。Runtime secret loader 嚴格拒絕 catalog 外欄位；部署前必須先從兩環境
`findb/<environment>/findb/runtime/configuration` 移除 `SHIOAJI_SKIP_NON_TRADING_DAYS`，
從 `findb/<environment>/fetcher/runtime/configuration` 移除 `SHIOAJI_SIMULATION`。
只建立刪除這兩個鍵的新 secret version，核對其他值逐一相同，保留舊版本供稽核；不得輸出 secret 值。
若未先清理，Backend 與 Fetcher 的唯讀 preflight 都會以 `secret_schema_invalid` 拒絕部署，
此時尚未 migration／切換 containers。確認 current release 與查詢健康後，清理欄位並重跑失敗 jobs。

Backend 驗收完成後，先審閱兩環境 OpenTofu plan：僅預期的 tag／IAM 更新與 Shioaji alarm 停用，
標準 CI plan 維持零刪除；不可刪除 secret、ECR、alarm metadata、RDS、S3、磁碟或 canonical archive。套用後以 instance role 驗證無法讀取
兩個 retired secrets，也無法拉取 Shioaji image；provider 端 API key 另行撤銷並記錄結果，不輸出值。
只改 Git 不代表雲端已套用或券商端 key 已失效。2026-11-09 兩環境部署與 IaC 撤權已驗收，
券商端撤銷依 Owner 確認；版本、保留性證據及限制見
[production 基準](production.md#shioaji-backend-退役部署基準)。

既有 EC2 user-data 保留原值，避免僅因移除空 cache 建目錄指令而觸發主機停止／啟動。
受保護 secret／ECR metadata、停用的 alarm metadata、checkpoint／cache／備份仍屬保留資產；實體清除需先完成資料與
接受紀錄保存、引用盤點及獨立 plan，不能使用 `force_delete` 或全域移除保護來完成退役。
例行 `apply_cutover` 的 host proof 只接受四個 active providers，但仍檢查舊 Shioaji owner／run／
historical 未結證據；不重新 attestation、不改寫保留的 work graph。

## Twelve Data HK 退役的 rollback 下限

Migration `c7d2e9a4f1b3` 只改資料，移除 `twelve_data` × `hk_equity_eod` 的 scheduler、未撤銷
credential 與 `allowed_sources` scope，並結清 open 告警。Fetcher 只接受 Source 回傳 US scope；
`fetcher-v0.1.16` 之前的版本與 Source 不相容，會以 `control scope mismatch` 停止。此後的 Fetcher
也不再容忍此 revision 之前的 Source，因此 Backend 不可回退到 `c7d2e9a4f1b3` 之前。
部署前以唯讀查詢確認此組合沒有未結證據，否則 migration 中止且不變更資料。成功 accepted 後此版本
即為新的 rollback 下限；downgrade 為 no-op，須回退時採 forward fix。

## Prepared 逐列儲存的 rollback 下限

Migration `9c1e4f7a2b6d` 將 `provider_work.prepared_deliveries` 搬到 `provider_work_prepared` 並刪除
舊欄位。部署前先以唯讀查詢確認每筆 entry 都是 object、`body` 若存在必為字串；否則 upgrade 會整批
失敗並保留原 schema。Backend 先部署；Fetcher 協定不變，可在 Backend 後更新。
成功 accepted 後此版本即為新的 rollback 下限：workflow 不執行 `alembic downgrade`，較舊 accepted
bundle 遇到新 revision 會拒絕啟動。須回到更早版本時，依上節採 forward fix，或在 controls stopped、
consumers 排空後以已核對備份復原；migration 的 downgrade 只供停止狀態下的人工復原，執行前須有
同一停止時點的 RDS snapshot。部署前產生的 Shioaji repair 預覽與休市結清盤點 digest 改以新的
prepared descriptor 計算，部署後會以 stale preview 拒絕，須重新取得正式預覽。

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
以下為 staging 既有切換紀錄；當時初次 runtime 部署維持 `FULL_MARKET_ENABLED=true`、`PROVIDER_WORK_REQUIRED=false`。
2026-10-20 的後續啟用已核對 59 筆 prepared request 的 checksum、Source 202 receipt 與 DB terminal，
十五個 consumers 均 graceful stop／exit 0。Checkpoint 成套備份位於兩台 staging host 的
`/var/backups/findb-provider-work/staging-20261020T1230Z`；Fetcher archive SHA-256 為
`cb80468e204c3b5aa296409b98cb56c5b2f2c6030d93552d94b49ae8d6d84829`，RDS snapshot
`findb-provider-work-staging-20261020-1230` 已 available。備份標籤不作實際擷取時間；
Fetcher 擷取於 12:20:28 UTC，RDS snapshot 建立於 12:20:39 UTC。

八份既有 SQLite 的原 tables／rows 比對一致，五組 shared-inode lock proof 通過，Owner cutover
已提交。兩端 AWS runtime config 與 deployment parameter 僅將 `PROVIDER_WORK_REQUIRED` 改為
`true`；舊 secret versions、原停止容器與 account usage 均保留。

2026-10-20 staging Backend 與 Fetcher 部署至 `f7d0d8f7c6770ef1c874b8036ec112bcd18d5b5d`：

- [Backend CD 39443577634](https://github.com/FPI-TW/findb/actions/runs/39443577634) 成功；七個
  containers running、零重啟，公開 Dashboard 兩次完整回應正常。
- [Fetcher CD 39446053008](https://github.com/FPI-TW/findb/actions/runs/39446053008) 成功；十五個
  consumers 均 accepted、running、零重啟、enforced，images 與 manifest 一致，五組 lock inode
  與原 Owner attestation 完全相同。FactSet image digest 為
  `5f89f291c6994db6f5e6a5db67f53bdd74dffde5c2d3784a222f80c81ce0b114`。
- 部署後再次核對 59 筆 Source 202 receipt／terminal／checksum，以及原 18 plans、18 parts、
  38 members、33 decisions、14 first-date states；皆與切換前證據一致。
- Accepted Backend bundle 為
  `findb/accepted/f7d0d8f7c6770ef1c874b8036ec112bcd18d5b5d/fd5a8a469e924e1b8329937c67aeba006f352fec5f708f404ca42fa0a933b1a9.tar`；
  Fetcher bundle 為
  `fetcher/accepted/f7d0d8f7c6770ef1c874b8036ec112bcd18d5b5d/90e062834a301f25ba7ec02e9bf5dc61baaf908563379ce6c507a17f4acb33d1.tar`。

[PR #322](https://github.com/FPI-TW/findb/pull/322) 修正測試時鐘，
[PR #323](https://github.com/FPI-TW/findb/pull/323) 修正 owner RPC 與最後本機 dispatch grant 的順序；
完整 Required CI 通過，Backend 2,564 tests、Fetcher 1,627 tests 通過。新版 Fetcher 需要新版
Backend 的 owner 到期證據；rollback 下限與完整契約見
[provider-work 授權](../architecture/provider_work.md)。首次新版 Backend activation 因公開探測
收到部分內容後逾時而 fail-stop；先以舊 accepted bundle 恢復並核對健康，再以上述成功 replay
啟用同一新版 bundle，未放寬健康門檻。這次受控復原發生在新版 Fetcher 部署前。

原 FactSet maintenance `01a15980-fb91-7a11-a121-ea63dbb329ca` 的第 5 次 attempt 已實際取得
HKEX 官方回應；但 2026-10-20 15:23 UTC 取得的檔案標示 `Updated as at 21/10/2026`，
晚於當時香港日期，因此 parser 以 `HKEX snapshot is future-dated` 拒絕，工作安全結算
`failed / MAINTENANCE_FORMAT_OR_SCOPE`，owner 已釋放。1,362,431 bytes 原始回應保留於
journal，其 SHA-256 為 `19fc424f77714fedcbd5fe9b398d50dd9372b18101eac9bf07e7f6be8afeeddb`；
當次未建立 HKEX release、HK provider cutover、FactSet RDS 查詢或行情 ingestion run。
原 failed 工作、journal 與預算保留；正常日期的後續結果見下節。

跨日清單更新、第一份 FactSet daily plan 的 frozen membership、prices SELECT、兩檔單日與
歷史全名單 R2 → Source → queue → canonical → Serve 驗收仍未完成。Twelve Data／FactSet
的四個 Pilot／Full controls 均 `stopped`；使用者選定的驗收後日常模式為 Pilot 每日兩檔、Full 停止。
Daily 新 I/O 仍限香港時間 18:30–23:00，歷史回補不能替代 daily 驗收。
Production 基礎設施尚未套用。
舊 bundle v1 仍可供升級前安裝辨識；
新版 source bundle 與 topology marker 必須一致，promotion 必須含四個 immutable digests。

Backend expand migration `d75fc12a9b43` 保存 request-time HKEX cohort 與 partial 狀態；
`FACTSET_SKIP_NON_TRADING_DAYS`（預設 true）及 `EOD_LONG_GAP_TRADING_DAYS`（預設 5）
由 runtime/configuration 的 optional keys 注入。既有四個日曆開關的必填與預設規則不變。
Native 驗收、明確 HK provider 切換與 rollback 證據限制見 [FactSet runbook](factset_hk.md)。

### 2026-10-21 staging HK 切換

HK 切換時 accepted Backend 為 `fe29ce5af468194aafbe39ea57a921f7104b1ccd`，bundle
`b7d191d09124f57cff68e2f1267fca5eb9a12afab05b702611bd9f5073d0f3f0`；Fetcher 仍為上列
`f7d0d8f`／`90e06283…`。兩份 S3 acceptance records 均為 schema-v3、staging／accepted，
images 與主機 manifest 一致；Backend revision 為 `d75fc12a9b43`。Rollback 保留 owner 到期
證據、v2／cohort／partial 與五 provider inventory，不能回退到不支援這些契約的舊版。

今日正常 CLI maintenance `01a15c37-5fbe-78f7-9623-b78a8a1591e8` 已 completed，建立並發布
HKEX release `01a15c37-822e-7131-8530-9cf37a29ca3e`，effective date 為 `2026-10-21`：

- 官方檔案 1,362,431 bytes，SHA-256 為上列 `19fc424f…afeeddb`，日期當日有效。
- 2,824 members；2,770 mapped，54 gaps（含 29 classification gaps）全部保留；member SHA-256
  為 `4a212bacc815926087f8862da812af380c27a7be86dbd06d1f53117570255720`。
- Staging FactSet RDS 原生 catalogue SELECT 回傳 2,796 rows／343,402 bytes，SHA-256 為
  `5b43601bf7732be173c715ff2060f91ab56512d927b4020d919355c21f0c87e1`，已保存在原 journal／raw。
  實際 image 的 CA bundle、`CERT_REQUIRED`、hostname verification、read-only transaction 與
  30 秒 timeout 均已核對；此為 catalogue 連線驗收，prices SELECT／行情交付仍待驗證。
- 以具名 SSM operator 執行同 immutable Backend image 的獨立 one-off publication，沿用既有
  publish／audit services；發布前 rollback dry-run 通過，audit 為
  `01a15c3c-f919-7ce2-8786-ae167c86e121`。

Twelve Data Pilot 的 running／stopped 是實際重啟故障：恢復 10/15 的 legacy HK daily work 時，
dataset 已不在 `installed_feeds`，feed lookup 拋出 `StopIteration`，owner 未結算。已透過
既有 Owner session 將 control r34→r35 stopped，停止三個 Twelve Data consumers，取得原
shared inode `staging:66305:512066` 後，以正常 Source recovery／settle protocol 將
`01a13f28-240e-7178-b068-143f3d3adb31` 結算 `failed / RETIRED_FEED_MANUAL_REVIEW`。
原兩名成員已是 manual，DB／checkpoint 均為零 prepared、零 receipt；原 frozen plan 與 gaps
不變。10/16 的 HK work 仍 suspended／queued；在修正退役 feed 處理前不得啟動 Twelve Data control。

兩 provider owners 已釋放，四 controls 的 desired／observed 均 stopped，HK cutover script
先 dry-run 再於 `2026-10-21 02:19 UTC` apply，記錄不可變 FactSet 決定、priority -1／0 與 audit。
切換前後 14 first-date states、20 plans、20 parts、42 members、35 decisions 的完整 fingerprints
完全一致。這項檢查不代表尚未建立的 FactSet daily plan 已驗收跨日成員凍結。

備份位於 Fetcher host `/var/backups/findb-hk-cutover/staging-20261021T0215Z`，包含 Pilot／Full
SQLite、provider-work journal 與 account governor，均通過 integrity check；原 lock inodes 不變。
RDS 為 encrypted／available、保留 10 天，操作前核對可復原時間為 `2026-10-21 02:03:13 UTC`，
既有人工 snapshot `findb-provider-work-staging-20261020-1230` 仍 available。不得將 governor
還原成較舊用量。三個原有 Twelve Data containers 已恢復，四個相關 controls 繼續停止。
最終核對十五個 consumers running，三個恢復的 Twelve Data containers 均零新增重啟；兩個
owner slots 為空、shared inodes 相同。10/20 failed maintenance 的 operation budget 仍為
5，10/21 正常新工作的 budget 為 1，沒有重置原工作。

SSM 證據：maintenance `dff4c933-2ea7-40df-ba14-7e89fb32b455`、publication
`5489661e-6c3a-44cc-ba6e-be751a31fd8c`、owner recovery `ad46fa9d-f898-48b2-a694-29c0297cf614`、
cutover dry-run `887ec9a1-3b39-44e1-9db9-429894e2501c`／apply
`aba70d2e-cefd-45d0-8432-b819866dbf11`、切換後 fingerprints
`c23a46b9-43e4-479b-8530-0b85064ec6cd`、恢復後健康／預算
`1a99f75a-7cc6-4c77-a64b-a0774e86cf12`、最終 owner／control／fingerprints
`b58add61-d89b-41c0-a39a-aba304fea429`。後續仍須另一正常日期的 maintenance／release，以及
合法窗口內的 FactSet daily 與 native prices 驗收，不修改日期、製造 no_data 或重置預算。

退役 feed 修正已加入 unified runtime：先檢查目前安裝範圍，原 daily／maintenance 只排空
既有 prepared，再以 `RETIRED_FEED_MANUAL_REVIEW` 結算；無法核對的本機 prepared 保留
owner。相容 scope 不變，closed round 與 frozen `scope.feed` 也不能恢復退役 provider I/O。
本機驗證為完整 Fetcher **1,642 passed**、相關 Backend **151 passed／3 skipped**，以及
全專案 format／lint／type check 通過。三項 skip 為既有選用原生驗收，不列為通過。
真實 Source／worker 回歸測試涵蓋原 bytes 重播、normalization terminal 前持有 owner、
FIFO 釋放與 Full 維持停止；staging 新版本部署與 Pilot 恢復的現場證據見下節。

#### 退役 owner 阻擋部署的復原

#326 已由使用者合併至 `dd1dece3e97c241e1a5f1ab2f87d097f58ae7040`。
[Fetcher CD 39536143222](https://github.com/FPI-TW/findb/actions/runs/39536143222) 的測試、images
publish 與 bundle preparation 通過，但 attempt 1 於 `2026-10-21 03:17 UTC` 在唯讀 preflight 被
`fetcher_host_eligibility=failed` 擋下；未執行 candidate acceptance／activation。
SSM command 為 `fa85a390-43fc-45a7-84a6-cba75316ff00`。當時 current 仍為 `f7d0d8f`，
Twelve Data Pilot container 正以 exit 1／restarting 反覆恢復 10/16 HK owner。
其 accepted image、環境、完整 argv 與 manifest 均正確，其他十四 consumers 通過 eligibility。
Control 已 stopped，既有 owner 的 drain 仍會碰到舊 feed lookup；只停止 control 或重試部署不足以復原。

使用者授權主機復原後，停止三個 Twelve Data consumers 並確認 PID 為零；在
`/var/backups/findb-hk-cutover/staging-20261021T0324Z` 保存 Pilot／Full SQLite、account governor
與 provider-work journal，各檔 SQLite backup／integrity check 通過，權限保留 0700／0600。
三個 consumers 使用原 containers／images／mounts，shared inode `staging:66305:512066` 未替換。
DB 確認 10/16 HK work 沒有 manifest、prepared 或 receipt；checkpoint 兩個成員已是 manual，
沒有 prepared body，逐 sequence receipt 亦為零。以正常 Source recovery／settle protocol 於 `03:24 UTC` 將
`01a14443-ca90-766c-b607-b669b7db0c0c` 結算 `failed / RETIRED_FEED_MANUAL_REVIEW`，
owner generation 176 後釋放，沒有 provider I/O。不得依這次零 prepared 條件泛化為自動清除 owner；
若有原 bytes／receipts 或無法核對，須先排空／復原並保留證據。

復原前後 HK 的 2 first-date states、2 plans、2 parts、4 members、5 decisions fingerprints 完全一致；
原 manual／missing 不改成成功或 no_data。十五 consumers running，恢復的三個 Twelve Data
consumers 零新增重啟；FactSet 10/20 與 10/21 maintenance budgets 仍為 5／1。
所有 Pilot／Full controls 均為 desired／observed stopped，未改動其 control revisions。

以原 workflow preflight 程式、相同 candidate bundle／SHA／images 重新執行完整唯讀驗證，
於 `03:26 UTC` 取得 `runtime_secret_loader=validated`、`fetcher_host_eligibility=running` 與精確成功 marker。
Candidate bundle SHA-256 為 `66a1826a11bc814a5b3ca37958d18dbab3ee54f5397aff615d2c17be86e23980`。
此結果證明原 preflight 阻擋已排除；之後使用者另行授權重試正式部署與驗收，結果見下節。

SSM 證據：停止 `91123c7d-b22e-4cc0-a9b6-ecbb1808e09e`、備份
`50a2fea0-02ee-4f1c-a20f-53a1f9425f88`、owner recovery
`20e33878-2d82-497f-8fa8-22fdc7d1d1f1`、恢復
`cd1ed9df-f8f9-42ae-a310-55c267be5844`、完整唯讀 preflight
`953cade6-ad7b-4372-ace6-b752c9e29865`、fingerprints
`8b087ff2-618c-4b1b-bcd2-20d519e7de2b`／`caa52862-d0c7-4657-9085-3b879aa0eca5`、
健康／預算 `f8600ff1-881f-432a-809f-f7df5c8d16a1`、所有 controls 停止
`6de138d3-6088-408b-a9ec-1a5ba93a4b5a`。


#### 部署重試與 Twelve Data Pilot 恢復

同一 [Fetcher CD 39536143222](https://github.com/FPI-TW/findb/actions/runs/39536143222)
attempt 2 已成功，candidate acceptance SSM 為 `5da17277-134e-405e-99d0-abb637ef9f59`。
`03:36 UTC` 發布 schema-v3、staging／accepted 紀錄：
`fetcher/accepted/dd1dece3e97c241e1a5f1ab2f87d097f58ae7040/66a1826a11bc814a5b3ca37958d18dbab3ee54f5397aff615d2c17be86e23980.acceptance.json`。
Bundle SHA 與原 candidate 相同，validator SHA 為
`344a79c77aeafbd790aae299b8725eefa854de3112069db993320ef572a92003`；
activation 後 current 為 `66a1826a…-39536143222-2`，commit 為 `dd1dece`。
十五 consumers 的 accepted label、環境、image 與 manifest 一致，全部 running／exit 0，重啟次數為零。
Twelve Data image 為 `sha256:404533a7dec6fbb9afde9de86de16079c43eed6c38849e6a878e43de97265ea2`；
實際安裝 feed 只有 `us_equity_eod`，退役處理函式 fingerprint 與已測試版本相同。
主機驗收 SSM 為 `f8f7a989-699d-4451-ae86-10feba46ea43`。

`03:44 UTC` 透過既有 Owner Dashboard 只啟用 `pilot_twelve_data_v1`，revision 37 → 38，
admission `01a15c8f-c6a4-75f3-80f7-81bbca47d2a2` 只納入 US baseline
`01a15c43-b72e-75f8-829c-5220b0cd9c56`。Source authorization 確認 Pilot acquisition allowed，
admitted keys 只有 US；Full acquisition 不允許，其他 Pilot 與全部 Full controls 保持停止。
HK 的相容 scheduler scope 保留，Dashboard 以 `provider_acquisition_cutover` 排除新抓取。
授權核對 SSM 為 `b73f0373-6d54-4a91-b320-51dfedd667dc`。


Pilot 持續完成正常 cycles，`03:44:51`、`03:45:23`、`03:46:33`、`03:47:05 UTC`
均有完成時間及新 heartbeat，`last_error` 為空。Cycle 完成後 `observed=stopped` 是 CLI
回報閒置的既有契約，需合併 `desired=running`、新 heartbeat、cycle 與資料證據判讀。
US 10/15–10/19 的舊工作依原 canonical／calendar 證據結算，未重新抓取或假造 no_data。
10/20 新 plan `01a15c90-cb27-7e8b-9f3e-42327e609bdf` 凍結成員 `A`、`AA`，
expected=2，member SHA 為 `e579f3789ae1cf09da44373fc2906adb037e21a10a15678c396bd5c464513392`。
兩個新 runs `01a15c90-d39f-71e9-ab34-07c70da05de0`／`01a15c90-d8ce-7a9b-8aa9-7fff591aee3e`
於 `03:45:57–59 UTC` 完成，各 1 record 成功、0 failed，各有 canonical member evidence。
Daily work `01a15bed-12eb-7c2f-bb82-131dab9d54c5` 在 normalization 後完成第二輪結算，
保留兩份 receipts，owner 已釋放；Dashboard 顯示 feed 1/1 complete、涵蓋日／預期日均為 10/20。
Dashboard Lookup 經正式 Serve path 顯示兩檔最新 EOD 均為 `2026-10-20`，
A／AA 明細的當日成交量分別為 1,978,600／5,735,300；未繞過 Serve 認證。
自 `03:30 UTC` 部署與恢復觀察期間沒有新增 Full ingestion runs。
資料流程驗收 SSM 為 `918801cd-d55e-4b0b-b3a2-d6c6f0bc8c8f`／`58622cca-26da-44fa-a5c8-da24a6e2339a`。

部署及 Pilot 恢復後，HK 兩筆 work 仍為 `failed / RETIRED_FEED_MANUAL_REVIEW`，
HK first dates／plans／parts／members／decisions 的前述 fingerprints 仍完全一致；owner 無 active attempt，
shared inode 未變。十五 consumers 仍 running，Twelve Data 三個 consumers 均零重啟，
FactSet 兩筆原 maintenance budgets 仍為 5／1。SSM 為
`365c4630-b04c-40f1-a1df-3b66a0a0670a`／`49c125ec-8321-443b-a355-82776724b973`。
FactSet 原生 prices／daily 及跨日 release 的未完成驗收仍由 backlog 追蹤。

### 2026-10-22 staging historical FinLab OOM 阻擋部署

#344 合併至 `6b153e734cc104d8a31302baa90d9eff8266d25e` 後，
[Fetcher CD 39709175450](https://github.com/FPI-TW/findb/actions/runs/39709175450) attempt 1／2
均在唯讀 preflight 收到 `runtime_not_eligible execution_profile=historical provider=finlab`；
candidate acceptance／activation 未執行，current 仍為 `226c9860…-39635077338-1`。
原因不是新 bundle：`findb-historical-finlab` 的 image、argv、環境與 accepted label 均正確且 running，
但 `State.OOMKilled=true`。主機 kernel 於 `2026-10-22 00:46:38 UTC` 發生 global OOM，
殺掉該容器內約 465 MB anon RSS 的舊版 fork 子程序（FinLab `data.get` 整張表載入）；
主程序未退出，Docker 仍保留 OOM 標記。Staging fetcher 主機為 `t3.small`（1.9 GB、無 swap），
十五個 consumers 常駐約 55–130 MB／個，部署前可用記憶體約 225 MB。

使用者確認 historical FinLab scheduler 已停止後，僅 `docker restart findb-historical-finlab`
清除標記（`OOMKilled=false`、exit 0），未改 image、mounts 或 checkpoint；attempt 3 成功，
current 為 `3e917404…-39709175450-3`，十五 consumers running，FinLab／FactSet images 為
`sha256:ede5e2aa…`／`sha256:ee1a2137…`。部署成功不代表 provider 功能驗收。

FinLab SDK 1.5.7 的 `data.get` 只接受 dataset 名稱，Pilot 只取兩檔也會在 worker 下載並載入整張表；
worker 只回傳 reviewed 欄位與完整表的 response bytes，避免父行程再持有一份完整表，但單次 worker 峰值仍在。
因此 staging 只驗證 Pilot 與少量回補，完整 Full market 與完整回補在 production 執行。

同日唯讀檢查 production fetcher（`t3.medium`，3.8 GB、無 swap）：十一個舊版 consumers，
可用約 1.2 GB，`findb-full-market-finlab` 常駐 1.26 GB，10/1 後沒有 OOM 紀錄。
Production 版本落後，運行問題延後處理；先以下列方式降低短暫高峰風險：

- 兩環境 fetcher 主機由 OpenTofu `aws_ssm_association.fetcher_swap` 執行
  `infra/deploy/ensure_swap.sh`，維持單一 4 GiB `/swapfile`（寫入 fstab、`vm.swappiness = 10`），
  可重複執行。須在各環境 `tofu apply` 後才生效。
- Staging 的 FinLab consumers（Pilot／Full／historical）由 `release_fetcher_provider.sh` 加上
  `--memory 1g --memory-swap 2g`，candidate 與 activation 都核對 `HostConfig.Memory`；
  其他 provider 與 production 不設上限。下次 staging Fetcher 部署重建容器後生效。

容器上限讓 FinLab 的 OOM 只影響該容器，但 `OOMKilled=true` 仍會被 preflight 拒絕部署；
再次阻擋時依本節確認原因，經授權重啟容器後重試。

## Provider catalog PR3＋4 production 驗收

2026-11-10 唯讀驗收已部署 release `d86b633aae3ac83cdb0ab1f789057d16d28b1f2c`：
[FinDB CD 42742153828](https://github.com/FPI-TW/findb/actions/runs/42742153828)、
[Fetcher CD 42743102997](https://github.com/FPI-TW/findb/actions/runs/42743102997) 均成功。
Backend／Dashboard images 與 manifest 一致；Fetcher bundle v4 三 images 與 staging accepted
digests 相同，12 個現役 consumers 無缺漏、額外容器、OOM 或重啟。S3 immutable decisions、
acceptance 與兩個 host committed receipts 相符。

四 providers × Pilot／Full 八組 execution-config 通過 Source auth、scope、environment、schema、
digest 與 no-store 核對；安裝 image 的 resolver、daily／historical `--check` 通過，舊排程檔不存在，
FinLab historical 仍只開放 equity。Admin 查詢前後 13 controls、7 first dates、142 frozen plans 不變；
未建立 production historical requests，未更動 running 的 Twelve Data／TAIFEX Full controls 或
其它 stopped controls。FactSet native 終態仍依 Owner 指示排除，不宣稱完整四-provider 終態驗收。

既有 2026-11-09 Twelve Data work `01a1c253-84ef-7e4b-a097-ac62c9f34057` 持續推進：checkpoint
快照為 5,408 data、10 source-proven no-trade、124 no-data retry、66 mapping gap manual、
1 normalizing。這是執行中快照，不是全 feed completed；mapping gap 留待資料品質處理，
本次未重抓、重寫或取消工作。Release／bootstrap／設定發布相容性通過，daily 完整性不在此聲明內。

| 證據 | SSM command ID |
| --- | --- |
| Backend release／receipt／健康 | `5fa58caa-9d63-4d24-83d8-9577e8d515a1` |
| Fetcher inventory／八組快照／bootstrap | `888a08ec-91f4-43c2-bbdc-dd8055fa8940` |
| Admin read-only／frozen snapshot | `83325eba-d6e8-49fc-bc48-83f5abfaf60a` |
| Daily checkpoint 唯讀計數 | `fb8e6e2d-2de3-47e2-ba23-56239fe0f118` |
| PR5 production 唯讀 preflight | `024a8366-db91-440e-85db-d168ae944ba7` |
| PR5 staging 唯讀 preflight | `58e386eb-1f12-4dbb-8cfe-dea19e484204` |

## Provider catalog PR5 發布與遷移

兩環境 PR5 唯讀盤點均為 11 筆 known_old_default、2 筆 already_compliant、0 筆
unknown_difference；此快照不能取代發布時重跑 preflight。Production 已發布 `findb-v0.1.24`，
見 [PR5 production 發布與唯讀驗收](#pr5-production-發布與唯讀驗收)。

既有環境的升級基線是已完成 Shioaji 退役的 `d8e3f0b5a2c4`；不支援在同一次
candidate deployment 才從退役前版本補做退役。正式 image 的 container regression 必須
實際驗證乾淨安裝、上述基線升級及重跑冪等，並驗證退役前 `a42c89d3e610` 被 preflight
阻擋且 revision、dataset config、controls 完全不變。一般 `pnpm test` 未設定
`FINDB_TEST_BACKEND_IMAGE` 時會跳過此 suite，不能視為已完成 container migration 驗證。

1. 確認目標已接受 PR3＋4 bundle v4，遵循既有 maintenance window、controls 停止／排空流程，
   記錄原先 desired state。先以 candidate image、既有 migration credential wrapper 執行
   `python /app/scripts/preflight_catalog_schedule.py`。CLI 使用唯讀 transaction，exit 1 表示
   未知差異，須先查明；不直接覆寫，也不輸出 secrets 或 operator notes。
2. Schema-v4 coordinator 的 candidate one-shot migration block 再次執行同一 preflight，
   成功才執行 Alembic、exact revision check、privilege／credential／registry reconciliation。
   Activation／replay 仍只核對 exact revision，不重跑 migration 或 provisioning。
3. `e9f4a1c6b3d5` 內嵌固定值，不 import live catalog；鎖定並驗證全部目標 rows 後才更新，
   未知差異中止 transaction。只改五條 feed 的 schedule／bounded deadline 表示、八個現役
   controls 的時間／時區。保留 enabled、action、threshold、notes、slot、desired／observed
   state、revision、first dates、既有 plan／work／receipts；retired rows 不動。乾淨安裝的 TW equity
   缺少 schedule 時，只接受 migration chain 所產生的完整固定 policy 形狀，其餘缺漏仍中止。
4. US 改為紐約 17:00、lag 0；FactSet 香港 18:30；FinLab 台北 18:00；TAIFEX 台北 17:00。
   Bounded US 仍是次日台北 09:15，TW equity 當日台北 17:00；其它 bounded monitors 保持 disabled。
   新 Pilot／Full plan 使用 US 次日台北 09:00，其餘當日台北 23:00。既有 frozen deadline
   是監控權威，不遷移或重建。
5. Staging 驗證 migration、market 分組、逐 feed 時間／時區、無 Slot 標示與 ingress 相容性後，
   再依 accepted digest promotion 流程另行安排 production。由 Owner 恢復原先運行項目，
   不自動啟用 stopped controls。PR5 後 Backend rollback 下限是支援獨立 deadline timezone
   的版本；migration downgrade 明確拒絕，資料修復採 forward fix。

### Staging 驗收清單

以下 staging 驗收尚未執行；production 已依 Owner 決定先行發布，不因此將本清單視為通過。

- 合併前確認 PR 最新 commit 的 Required CI 全綠，並處理 review findings。合併後由既有
  schema-v4 staging CD 建立 candidate，記錄 Backend／Dashboard digest、deployment attempt
  及 accepted bundle；不另起舊版部署流程。
- 維護窗口停止／排空後、migration 前，保存唯讀 baseline：五條 feeds、八個現役 controls、
  retired controls、operator policy、first dates、既有 plans 的 `deadline_at`、work 與 receipts。
  原先已停止項目保留停止狀態；有未知差異時不繼續 migration。
- Migration 後 revision 為 `e9f4a1c6b3d5`，重跑 preflight 應全部 already_compliant。
  比對 baseline，僅允許本節列出的 schedule／deadline 欄位變化；確認 seed 重跑不改回舊值，
  且 controls、operator policy 與 frozen evidence 保持一致。
- 核對四個 providers 的 pilot／full_market Source execution-config、Admin feed schedules
  與 freshness 時間／時區一致；Dashboard 依 HK／TW／US 分組，FinLab 顯示兩條 feeds，
  不出現 Slot 標示。既有 slot ingress 相容性仍須通過。
- 驗證既有 plan 採 frozen deadline、新 plan 採 catalog；US Pilot／Full 次日台北 09:00 與
  bounded 09:15 分開。歷史回補不新增 daily gate，依既有驗收紀錄追蹤 FactSet native terminal
  的使用者同意略過項目，不標記為通過。Owner 僅恢復原先運行項目後，記錄最終驗收結果。

### PR5 staging Fetcher 部署恢復

PR5 `8104e48af631821a9c82a2e2ccb7b5d31b761eae` 的
[FinDB CD](https://github.com/FPI-TW/findb/actions/runs/42750921117) 成功；
[Fetcher CD 42750921079](https://github.com/FPI-TW/findb/actions/runs/42750921079) 第一次因
FactSet historical 尚未排空，SIGTERM 後 90 秒被 Docker KILL（exit 137），以 `graceful_stop`
中止。原十二個 consumers 與 current 已恢復，transaction 為 aborted；不是 migration、
image build 或殘留 inventory 失敗。復原核對 SSM：`2a8d0998-6a22-428b-bcc2-5a724ea219ea`。

原 FactSet 請求自然結算後，確認無 active owners／queued 或 running historical requests，
八個 controls 均 desired／observed stopped，才重跑失敗 jobs。第二次嘗試成功，未改程式、
停止上限、controls 或 frozen cohort。現役 bundle 為
`69bd242775e85ba0aaf20af2fd342337488cfc04a16a1a9a45b310ad39ff7402-42750921079-2`，
transaction committed、十二個 consumers 皆為 accepted healthy、零 restart／OOM，三個 image
digests 符合本次 candidate。部署後 SSM：`9eb2e8bf-8e13-4cf9-afe0-d25f8f69f97d`。
此紀錄確認部署恢復；上方 PR5 migration／UI 的完整 staging 驗收仍須執行。

### PR5 production 發布與唯讀驗收

2026-11-10 依 Owner 決定 promotion PR5 `8104e48af631821a9c82a2e2ccb7b5d31b761eae`，版本為
`findb-v0.1.24`；上方 staging migration／UI 驗收清單未先執行，維持未完成。Fetcher 程式自
`d86b633aae3ac83cdb0ab1f789057d16d28b1f2c` 未變更，production 維持 `fetcher-v0.1.28`，未重部署。

發布前唯讀盤點（SSM `7e715b8f-e1e9-4b2e-be73-0e1972ac4c6a`，`SET TRANSACTION READ ONLY`）：
revision `d8e3f0b5a2c4`；preflight 為 11 筆 known_old_default、2 筆 already_compliant（兩個 FinLab
controls）、0 筆 unknown_difference。八個現役 controls 已為 desired／observed stopped，先前
Twelve Data／TAIFEX Full 的 running 狀態已不存在；無 queued／running historical requests 或
provider work，attempts 均 settled，normalization 全部 completed。排空條件成立，不需另停 controls。

[FinDB Production CD 42767771254](https://github.com/FPI-TW/findb/actions/runs/42767771254) 成功：
verify、promotion、read-only preflight、apply、acceptance 與 finalize 皆通過，未觸發 rollback 或
`DeploymentFailure`。扣除 verify 226 秒後部署淨耗時 **240 秒**（總牆鐘 466 秒）。
Production accepted bundle：

```text
findb/production/accepted/8104e48af631821a9c82a2e2ccb7b5d31b761eae/b4f07bf17489dba6baad26fad6e439b0f783df03e530ae4a7a8881ffe1cef916.tar
```

部署後唯讀驗收：

- Revision 為 `e9f4a1c6b3d5`，重跑 preflight 13 筆全部 already_compliant（SSM
  `1d3b1429-75e9-423d-9983-864cc513a4cf`）。
- 八個現役 controls 仍為 stopped／stopped，未自動啟用；時間／時區與 catalog 一致：Twelve Data
  紐約 17:00、FactSet 香港 18:30、FinLab 台北 18:00、TAIFEX 台北 17:00。
- Admin schedulers 與 market-freshness 在 Pilot／Full 各回傳五條 feeds，扣除 `next_scheduled_at`
  後與安裝 image 的 catalog 描述完全相同；freshness 市場為 HK／TW／US。Provider metadata
  SHA-256 仍為 `e6a89f5b6f509ef73098c9109e52d11485bfc30a2bcc846ed77f2a6471c7eb44`。
  退休 TW minute datasets 的 delivery-plans 查詢均為 HTTP 200。
- GET 前後 read-only 快照相同：13 controls、7 first dates、142 frozen plans；plans 的
  identity／deadline／membership／expected count 指紋 SHA-256 為
  `7dc1ba7b4dc5a784cee10394dbd9eb7579f55283d868c122323da8f0c8564159`，work、historical、
  normalization 計數不變（SSM `79ba41e2-c619-4a10-b618-6d09401f7cd6`）。
- 公開 `/health` 與 `/dashboard/lookup` 均 HTTP 200。

PR5 accepted 後 Backend rollback 下限為 `findb-v0.1.24`；`e9f4a1c6b3d5` downgrade 明確拒絕，
資料修復採 forward fix。未驗證項目：Dashboard 實際畫面的市場分組與無 Slot 標示、新 plan 採
catalog deadline（需 Owner 恢復 controls 後產生）、既有 slot ingress 相容性（需 Source 寫入），
以及 staging 驗收清單。Controls 由 Owner 恢復原先運行項目，本次未變更。
