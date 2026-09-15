# Deployment Runbook

## 收斂後的 promotion 契約

`findb-ci.yml` 與 `fetcher-ci.yml` 是唯一的 unit test 定義。Required CI、staging CD
與 production promotion 都以已解析的 40 字元 SHA 呼叫它們，checkout 後再次比對
`HEAD`。`infra/deploy/change_policy.py` 是唯一的 changed-path truth table：contract-only
變更會跑兩個 CI，但不會自動部署；main 上與 unit 有關的變更才自動進 staging。

`findb-cd.yml`／`fetcher-cd.yml` 只負責 staging；recycle 期間仍保留已完成 live
acceptance 的 staging SSM transaction。Production promotion／replay 呼叫 target-aware
reusable deploy workflow；兩條路徑共用 target-aware host helpers 與相同的 candidate、
accepted-record、activation、health、rollback fail-closed 次序。新 release manifest 為 v2，記錄 target registry、
release tag；production manifest 另必須帶 staging accepted bundle 的 promotion source。
歷史 staging v1 accepted bundle 僅能 read-only replay，production 一律拒絕 v1。

Production 無 push trigger：operator 必須從 immutable `findb-vX.Y.Z` 或
`fetcher-vX.Y.Z` tag 手動 dispatch、依 unit 輸入 `PROMOTE_FINDB_PRODUCTION` 或
`PROMOTE_FETCHER_PRODUCTION`，且 Environment variable
`PRODUCTION_DEPLOY_ENABLED` 必須精確為 `true`。promotion 不重建 image；它以 Docker
Buildx registry copy 將 accepted staging digest 複製到 production repository、驗證目的
digest，tag collision fail closed，同 digest 則為 idempotent。rollback 只可選擇 production
accepted record。

每個 production unit tag 必須是 annotated tag；lightweight tag 在任何 reusable CI 前即 fail closed。
workflow 會在 production deploy-bundle bucket 的
`<unit>/production/release-tags/<tag>.binding.json` 建立或驗證不可變 binding。binding 以
SSE-KMS 與 `If-None-Match: *` 原子保存 unit、release tag、tag ref object OID、peeled commit SHA
及 exact staging accepted bundle key。重跑 promotion 只接受五者完全相同；conditional-put 競態會讀回並驗證
勝出的物件，絕不覆寫。rollback 不建立 binding，必須先用目前 tag OID 與 peeled SHA 驗證既有 binding，
再由 reusable deploy 驗證 production accepted record／bundle；因此 moved、recreated 或跨 unit tag 都不會進入 CI 或部署。

> 目前實際target是staging；production EC2與外部資源
> 尚未完成。Staging runtime secrets已由instance role從Secrets Manager載入，application image已由
> accepted bundle固定為exact digest；FinDB與Fetcher staging workflow均已改為OIDC＋SSM bounded
> candidate／accepted-record／activation，並完成各自的live acceptance，Phase 4與Phase 5 exit gate均已完成。
> Phase 6已完成完整native/custom alarm coverage、RDS PITR restore、encrypted root replacement、
> Session Manager recovery、SSH ingress與host recovery key退場、generated cache重生、Fetcher SQLite
> backup/restore、RabbitMQ volume rebuild、different-digest rollback及不同Alembic revision的schema拒絕。
> 兩個current root volumes已建立即時encrypted recovery snapshots；2026-09-08已將daily DLM
> schedule-only tag由重複的`Purpose`修正為`BackupPurpose`並完成zero-delete apply，policy回到
> `ENABLED`。2026-09-14回讀已確認至少五個雙volume排程週期，首兩個不同週期驗收完成。
> `staging-findb`與`staging-fetcher`的deploy SSH secrets均已刪除；production 使用 OIDC＋SSM，
> staging security groups已無TCP/22 ingress，兩個EC2 key pair與host recovery key material亦已退役；完整紀錄見
> [Staging AWS Deployment Completion Plan](../dev/staging-aws-deployment-plan.md)。

## Workflow與release units

| Workflow | Unit | Trigger | Environment／concurrency |
| --- | --- | --- | --- |
| `findb-ci.yml` | Backend、migration、Dashboard、contracts | PR由`required-ci.yml`路由；`workflow_call`、manual | 不讀deployment Environment |
| `findb-cd.yml` | Backend＋Dashboard | FinDB paths合入`main`時，在staging cutover gate啟用後自動rollout；manual dispatch只接受staging | `staging-findb`／`staging-findb` |
| `fetcher-ci.yml` | Fetcher、contracts、images | PR由`required-ci.yml`路由；`workflow_call`、manual | 不讀deployment Environment |
| `fetcher-cd.yml` | Generic＋FinLab＋Shioaji Fetcher | Fetcher paths合入`main`時，在staging cutover gate啟用後自動rollout；manual dispatch只接受staging | `staging-fetcher`／`staging-fetcher` |

Deployment concurrency一律`cancel-in-progress: false`。Caller以`<target>-<unit>`序列化整條CD，
reusable deploy job另用`<target>-<unit>-deploy`序列化host mutation；兩層不得使用同名group，避免
called job與仍持有caller lease的workflow自我競爭。CD直接呼叫同revision reusable CI；
不可用branch最近一次成功取代。Contract-only變更會執行兩個CI，但不自動部署任一unit；需要依backend-first順序manual dispatch。

FinDB deployment unit包含backend、Dashboard、nginx、RabbitMQ及Compose services。
Fetcher的三個provider images是另一個unit。staging在build／reuse後從ECR exact SHA tag取得並嚴格驗證digest，
產生 unit-scoped、canonical JSON release manifest artifact（FinDB兩張、Fetcher三張完整
`repository@sha256:...` references）。manifest 不含 secret，並記錄 allowlisted deployment
bundle checksum。image 的 `contract_versions` 直接由 selected source root 的
`contracts/manifest.json` descriptor-safe 嚴格讀取；每個 entry 指向的 selected schema 也以同一個 pinned
root FD 讀取。每個 selected relative path 第一次讀取後都固定為同一份 in-memory bytes snapshot，後續
contract parse、schema verify 與 source checksum 都不重新開啟該 pathname；actual SHA-256 必須等於 entry
宣告值，才計算並寫入該 manifest 的 canonical semantic SHA-256。
validate 同時重新計算這些來源並精確比對 workflow 傳入的 unit、selected commit、migration revision、run ID
與完整 unit image-ref map，避免另一份語法正確的 manifest 被替換後通過。
bundle checksum 現命名為 `deployment_source_bundle_sha256`，只描述 selected commit 的 deterministic
source/template inputs（包括 selected `contracts/manifest.json` 與 selected manifest tool 的內容 digest）。
它涵蓋與 staging host contract 相同的 unit-scoped sources，另加這兩個 manifest inputs；不含 current
CI-only ECR build helper、secret、env、production-only serve-key template／renderer。它不是 render 後 host
deploy bundle、accepted replay bundle 或 live deploy evidence。實際 versioned/rendered deploy bundle與
accepted-replay程式契約已完成合併後的normal deployment及replay live acceptance；目前accepted
evidence見本節後段。

Artifact 名稱為 `findb` →
`staging-findb-release-manifest-${{ github.run_id }}-${{ github.run_attempt }}`、`fetcher` →
`staging-fetcher-release-manifest-${{ github.run_id }}-${{ github.run_attempt }}`；每個 rerun 都會保留獨立
artifact，不覆寫之前的 attempt。

Manifest policy/tool 由 selected SHA own：workflow 只執行 selected source root 中的
`infra/deploy/release_manifest.py`，不使用 current checkout 的工具。selected commit 缺少工具、工具不是
單一regular blob、protocol不符或bundle驗證失敗都直接fail closed；不存在manifest-missing或裸
SHA-tag fallback。歷史staging v1只可透過同unit、同commit的immutable accepted bundle與acceptance
record唯讀replay。

workflow 不再額外 `git show` contract manifest；generate 與 validate 都傳入同一個 selected source root 的
`contracts/manifest.json`，避免第二份 materialization 漂移。`fetcher-cd.yml` 的 PR-only policy 變更同時
路由至 FinDB/backend CI，因此完整的 deployment/manifest contract tests 會實際執行；一般 `fetcher/**`
source 變更仍只路由 Fetcher CI。

artifact output 寫入只接受預先存在的 job-private runner temp parent，從 filesystem root 以 `O_NOFOLLOW`
逐段 pin 到 parent directory FD，再用 single-link temporary file 與 atomic replace；這可避免祖先／輸出
parent path 的 symlink swap 影響本次寫入。它不是、也不宣稱是對同 UID
任意持續寫入者的完整防護；GitHub job-private temp 與 workflow expected-input binding 是此 foundation 的威脅
邊界。

Phase 3 foundation artifact 已有兩個 unit 的 live acceptance。Fetcher manual run
[33244760600](https://github.com/FPI-TW/findb/actions/runs/33244760600) 產生三枚 digest manifest，
獨立 SSM command `56127a67-2eed-4535-a670-86faad915e21` 驗證三枚 host-local RepoDigest 完全一致，
且三個 scheduler 均為 running、restart count 0。FinDB manual run
[33245539837](https://github.com/FPI-TW/findb/actions/runs/33245539837) 產生兩枚 digest manifest並完成
部署；獨立 SSM command `b6cb3d90-61c3-4a9f-bd80-3ad854dadd69` 驗證 backend／Dashboard
RepoDigest、Alembic `d6e7f8a9b0c1`、RabbitMQ與所有public/internal health checks。這些證據只證明
artifact內容、ECR digest與當次SHA-tag deployment一致。

後續Phase 3使validated bundle中的完整digest成為staging deploy及rollback identity；SHA tag只作
build/reuse selected-commit索引。staging build bridge會把已驗證的unit-specific digest refs傳給deploy
job，bounded SSM preflight在任何SSH或writer interruption前以instance role、tmpfs Docker config pull並
逐一inspect exact RepoDigest。此路徑最初由共同merge SHA
`228989afe857c82d619cd53d15dbb29873d6710a`完成live驗收：FinDB run
[33246701516](https://github.com/FPI-TW/findb/actions/runs/33246701516)／SSM command
`d6437246-f584-4545-9224-88867b8bdef9`輸出`ecr_digest_pull_inspect=ok images=2`，Fetcher run
[33246700446](https://github.com/FPI-TW/findb/actions/runs/33246700446)／SSM command
`9e4596ca-1888-44bf-acef-89858f9b35d4`輸出`ecr_digest_pull_inspect=ok images=3`；兩者均為
`Success`／exit 0且後續部署健康。Compose/helper digest cutover與private S3 accepted bundle／record已於
2026-08-30完成normal deployment及replay live acceptance；production promotion已完成workflow與
dry-run契約，但foundation、AWS資源及live acceptance仍未執行。

## Staging data policy

Staging用來驗證部署、bounded端到端資料流與故障處理，不承載完整universe或
production-scale歷史資料。Active feeds與pilot範圍見
[現行架構](../architecture/overview.md#active-feeds)。

任何data-producing操作事前記錄：

- image／config SHA與operator；
- provider、dataset、symbols、日期、row上限及預估credits；
- pre-run counts、停止條件與cleanup／rollback方式。

驗收可覆蓋provider fetch、Raw R2、Source、outbox、RabbitMQ、normalization、DQ、
canonical及Serve／Admin／Dashboard lineage。不得以smoke名義擴張universe或執行歷史
backfill。Deployment不改寫DB scheduler desired state；啟用或停止由Dashboard Owner控制。

## Environment與設定

Staging orchestrator 與 production promotion workflow 已分離；production workflow 是預留
control plane，不代表 production Environment 或 AWS資源已建立。現況如下：

| Target／resource | Current state |
| --- | --- |
| `staging-findb` GitHub Environment | 已建立，為目前FinDB staging target |
| `staging-fetcher` GitHub Environment | 已建立，為目前Fetcher staging target |
| `production-findb` GitHub Environment | N/A（尚未建立） |
| `production-fetcher` GitHub Environment | N/A（尚未建立） |
| Production FinDB EC2 | N/A（尚未建立） |
| Production FinDB RDS | N/A（尚未建立） |
| Production promotion snapshot／dump／seed | N/A（尚未建立） |

Push至`main`依 shared change policy 執行 same-revision CI；相關 unit 自動 rollout 至 staging，
contract-only 只跑兩個 CI。unit-specific staging caller或reusable deploy workflow本身變更時，亦必須
rollout對應unit，以實際驗證變更後的deployment path；CI workflow、production control plane與其它
workflow-only變更仍不自動部署。staging manual dispatch 僅用於 exact accepted bundle replay。Production
只能從 immutable unit semver tag 手動 dispatch `promote` 或 `rollback`，不能由 push 選取 target。
兩者都不配置GitHub Environment人工核准，仍以PR、required CI、protected branch、immutable tag、
固定 confirmation與target隔離控制變更。每個job只能取得自己unit與target的設定；branch與Environment
protection的實際外部設定需另行驗證。

完整變數與secret名稱不在本runbook重複維護，以
[infra/env契約](../../infra/env/README.md)、各target的`remote.env.example`、workflow及
`app.config.Settings`為準。Sync script只新增或更新GitHub Environment，不會刪除退休值；
operator必須另行移除不用的repository／Environment設定。

目前staging runtime secrets由EC2 instance role依consumer allowlist讀取Secrets Manager。每次載入任何
consumer前，host loader會先在既有`/run` tmpfs安全建立或驗證
`/run/findb-runtime-secrets`：目錄必須是非 symlink 的目錄、`root:root`與`0700`；若`/run`不是 tmpfs、
路徑型別不正確或既有權限不安全便會 fail closed。這使EC2重開後清除`/run`時，下一次candidate、accepted
activation或受包裝的runtime載入可自動恢復空目錄，但不會修復或採用不安全的既有路徑。secret bundle仍只在
該root下以`0600`建立並於使用後清理。GitHub Environment的23枚舊runtime copies已於2026-09-09依明確授權
刪除，兩個Environment secret清單回讀皆為空；GitHub OIDC與service-specific deploy role已用於AWS preflight／control-plane；
FinDB與Fetcher staging日常deployment transport均已完成OIDC＋SSM live驗證；Fetcher的FinLab smoke亦使用
bounded SSM。`staging-findb`已移除`FINDB_EC2_HOST`、`FINDB_EC2_USER`、`FINDB_EC2_SSH_KEY`，
`staging-fetcher`已移除對應的三個`FETCHER_EC2_*` deploy secrets。後續Phase 6另已移除staging
TCP/22 ingress、兩個EC2 key-pair resources與host中的對應recovery keys；production 設計同樣使用
OIDC＋SSM，GitHub Environment 只保存非 secret control-plane configuration。Different-digest rollback與
schema-incompatibility rejection已在2026-09-03完成live演練。

## Credential與storage邊界

- FinDB與Fetcher使用不同deployment target、credential與secret scope。
- 每個provider/client使用獨立DB-backed Source key與dataset allowlist。
- Fetcher calendar使用獨立read-only Serve key，不得與Source key共用。
- Break-glass key只供bootstrap／recovery；日常Dashboard使用具名session。
- Fetcher只持有Raw R2 Object Read & Write；不得取得Canonical R2、RDS、RabbitMQ或Admin。
- Canonical publisher與reader credentials分離，且只注入worker與serve。
- R2 management credential不得注入application runtime。

Raw與Canonical使用不同private buckets。Raw object key由provider／dataset開始，contract
只帶credential-free `r2://` reference與checksum。Raw R2 lifecycle為30天、bucket lock
為7天；此Cloudflare設定已於2026-08-21人工確認。Canonical runtime尚未實作，不得做
資料面acceptance聲明。

Fetcher SQLite位於provider-specific EBS paths，owner為runtime UID，目錄`0700`、檔案
`0600`。Raw bucket binding marker不符時deployment fail closed；不同bucket的state與
prepared refs不得混用。Candidate通過preflight與single-writer檢查後才能取代stable。

## Network與GitHub protection

Repository可證實的application／Compose邊界與需要外部核對的target policy必須分開看待：

- **Operator pre-deploy**：確認RDS不公開且只接受FinDB EC2 security group，Fetcher沒有RDS
  route；未保存外部設定證據前不得宣稱已驗證。
- **Workflow**：RabbitMQ ports不對host公開；第三方Actions固定完整commit SHA；不同unit
  使用獨立deployment concurrency。同一個`main` SHA同時觸發FinDB與Fetcher staging rollout時，
  Fetcher deploy會先以GitHub Actions API等待該SHA的FinDB CD成功；FinDB失敗、取消、狀態異常或等待超過
  60分鐘時，Fetcher在取得AWS credential及變更host前fail closed。Fetcher-only SHA沒有對應FinDB run時
  會先做最多2分鐘的bounded discovery再繼續；manual dispatch仍由operator避免與FinDB部署重疊。
- **Operator pre-deploy**：確認Source經Cloudflare/nginx還原可信client IP後執行allowlist，
  TLS private key只存在FinDB target；外部Cloudflare、DNS與security group設定另行留證。
- **Operator pre-deploy**：確認repository政策與外部設定要求PR、required CI及protected
  branch。staging在cutover gate為`true`時由合併至protected `main`自動rollout；production只接受manual
  dispatch。兩者均不設GitHub Environment人工核准。

## Release順序

一般release：

1. **Workflow**：對同revision執行對應CI；Environment-bound `select`先驗證target／replay contract但不
   取得AWS身分，無Environment的`publish`再以main-ref publisher role build／reuse並push明確image
   identity，最後Environment-bound `prepare`以deploy role產生及上傳candidate bundle。Publisher role
   不得寫bundle，deploy role不得publish image；之後才由reusable deploy workflow pull candidate。
   Fetcher的generic、FinLab與Shioaji Dockerfile都從monorepo root複製`fetcher/`及`contracts/`，因此CI與
   staging publisher必須一致使用repository root作build context，並以`fetcher/Dockerfile*`選擇Dockerfile；
   `./fetcher`不是合法context，否則無法包含共享contracts。
2. **Operator pre-deploy**：確認target、backup／PITR、disk／inode、container state、觀察窗口及
   必要external dependencies；現行workflow未自動涵蓋的項目必須人工留證。
3. **Workflow**：執行remote與DB preflight；FinDB migration前停止本unit所有DB writers，
   由單一migration job升級。
4. **Workflow**：啟動candidate，檢查container、internal health、nginx、RabbitMQ topology、
   worker ping及DB-authoritative queue health。
5. **Operator acceptance**：從外部驗證public TLS／routing，執行bounded fixed-idempotency
   smoke並確認terminal state。
6. **Operator acceptance**：記錄實際image、Alembic revision、設定checksum及驗收結果。

Contract upgrade固定backend-first：**Workflow**先驗證Backend同時接受新舊版本並部署；
**Operator acceptance**完成schema與shadow delivery驗證後，再manual deploy Fetcher切換pin；
經過保留期才停止舊版。

Fetcher deploy保留DB desired state，不把deployment當成啟用授權。三個scheduler分別
執行offline preflight、SQLite quick-check、bucket binding與single-writer reconciliation。
每次staging rollout前，Dashboard Owner必須在Scheduler頁按「全部停止」，確認逐筆revision-safe更新完成，
並等待三個scheduler的desired與observed state都顯示`stopped`後再合併或manual dispatch；
workflow在stable container優雅停止後只回報stopped observation、讀回desired state與驗證definition，
不修改desired state。任一provider仍為`running`、definition drift或control失聯都fail closed並恢復previous
containers。Accepted activation完成後，由Owner透過Scheduler頁按「全部啟動」，再逐一確認observed state恢復
`running`；批次操作若部分失敗會保留每張卡片的錯誤，不會繞過人工判斷或自動重試。
Fetcher candidate只以`docker create --restart no`驗證最終container config，並以
`com.findb.fetcher.accepted=false`標示；未accepted scheduler從不啟動，因此untrappable command／host
interruption不會留下未授權writer。Accepted activation先以atomic symlink replacement將`/opt/fetcher/current`寫成
exact accepted release，作為durable desired-release journal，再啟動三個標示accepted的provider；
一般錯誤會同時恢復containers與舊pointer，hard interruption則以同一accepted key重播完成收斂。
常駐CLI將`SIGTERM`／`SIGINT`轉為shared stop event：idle時立即退出，final running preflight
後收到stop也不得啟動新provider cycle；已開始的cycle則完成terminal report後退出。Workflow
以30秒grace period停止stable container並要求exit code為`0`，否則fail closed並恢復原stable，
不得把exit `137`或其它非零退出視為成功promotion。首次signal-handler bootstrap已完成且
temporary allowlist已移除；後續所有provider與image一律套用相同的strict exit-`0` gate。

## Alembic migration

ORM與migration必須在同一PR；runtime只驗證revision，不執行`create_all()`。Staging採
forward-only；downgrade只供本機round-trip測試。

本機：

```bash
uv --directory backend run alembic current
uv --directory backend run alembic upgrade head
uv --directory backend run alembic revision --autogenerate -m "describe change"
uv --directory backend run alembic downgrade -1
uv --directory backend run alembic upgrade head
```

Autogenerate後人工審查schema/table、enum、constraint、index、data backfill、lock／rewrite、
partition及可逆性。涉及既有資料時在clone或partial dump驗證，不只測空DB。

`market_data_eod`的年度partition屬migration-owned schema state。只有使用
`MIGRATION_DATABASE_URL`的migration job可以建立或附掛partition；application runtime只以
PostgreSQL catalog唯讀確認目標年度的exact partition已附掛，不持有schema DDL權限。若缺少
partition，normalization以`EOD_PARTITION_UNAVAILABLE`永久失敗，operator必須先由Alembic
補齊schema再重送；不得為了恢復ingest而授予application role `CREATE`。年度邊界前應在
migration測試與staging preflight確認下一年度partition已存在。

Staging rollout：

1. **Operator pre-deploy**：確認backup／PITR與可用的restore紀錄，暫停provider並確認沒有
   未結束的delivery cycle。
2. **Workflow**：執行read-only DB preflight；失敗時不停止既有服務。
3. **Workflow**：停止`ingest`、`dispatcher`、`worker`、`raw-cleanup`及本unit所有writers。
4. **Workflow**：由candidate image執行單一`alembic upgrade head`與`alembic current`。
5. **Workflow**：啟動queue、worker、ingest及相容的serve，執行container、health與queue
   checks。
6. **Operator acceptance**：執行fixed-idempotency smoke，驗證terminal state後才恢復producer。

`alembic stamp`只可在schema已人工證明與revision完全一致時使用，不能掩蓋drift。

## Acceptance與rollback

### Staging deployment bundle（Phase 3 wave 1，已完成 live acceptance）

Staging release 會由 selected commit 的 stdlib `release_manifest.py` 產生 deterministic tar
bundle；內容只含該 unit 的 allowlist、契約 manifest/schema 與 release manifest。workflow 以
外部 SHA-256 將 immutable candidate 上傳到 private、versioned、SSE-KMS 的 deployment-bundle
bucket（`findb/` 或 `fetcher/` prefix），SSM preflight 先以 instance role 下載、比對 SHA、驗證
bundle/manifest 與 exact `repository@sha256` image refs，才會 pull image 或中斷 writer。

FinDB staging candidate 僅可在bounded checks期間啟動，成功回傳前必須直接停下固定的public／application／writer
containers，且不可變更`current` pointer。candidate成功後，workflow 才可把相同 bytes 寫入以 commit 與 bundle SHA
命名的 `*/accepted/` key；strict acceptance record 才是 accepted 的 commit point，只有 bundle 而無等價 record
不可 replay。record存在後才可送出另一個bounded SSM activation command；activation只接受DB已在exact selected
Alembic revision，絕不再次migration。條件寫入遇到既有
bundle時會重新驗證 SHA，遇到既有 record 時只接受相同 immutable identity，因此可安全完成 orphan bundle 的
record。失敗 run 不得寫入 accepted evidence。replay 必須指向同 unit 的 accepted immutable key，不能使用
candidate、latest 或跨 unit key，也不可重新產生 manifest。Phase 3的兩個unit normal accepted deployment與
replay live acceptance已完成；FinDB與Fetcher日常host transport已分別在Phase 4與Phase 5切換為SSM。

`AWS-RunShellScript`原生以`/bin/sh`解讀command；workflow因此先以
`infra/deploy/ssm_bash_command.py`把完整host script轉為POSIX-safe wrapper，再由wrapper
`exec bash -c`。preflight、candidate／replay、activation與FinLab smoke都必須走這個入口，確保
`set -euo pipefail`、`ERR` trap與Bash quoting在staging／production一致生效，不能直接把Bash script交給
`AWS-RunShellScript`。

Host preflight的runtime-secret validation必須把一次性env寫入
`/run/findb-runtime-secrets/preflight-<unit>/runtime.env`並使用`--check-only`，由loader確認tmpfs、owner、mode與
secret schema後立刻透過已開啟的directory descriptor刪除；不得改用`/tmp`或release work directory。Compose在
FinDB preflight只以`config --no-interpolate -q`驗證結構，runtime interpolation與secret injection只在後續受
包裝的candidate／activation helper內執行。Fetcher bundle刻意不包含FinDB Compose；其preflight改以`bash -n`
驗證bundle內三個runtime shell helpers，再由candidate／activation執行provider transaction。

Staging的non-secret application設定仍以unit GitHub Environment variables為source of truth；reusable
deployment workflow必須用固定allowlist逐項shell-quote，並在candidate與activation兩個SSM command傳入完整契約。
FinDB另在runner端要求`PORT=8080`，避免與Nginx upstream契約分歧。Fetcher保留既有bounded numeric／provider
defaults，但environment-specific URL、R2 account與bucket仍不得缺少。Production不沿用這些GitHub application
values：workflow傳入空外層值，instance-side runtime-secret wrapper再從
`findb/production/<unit>/runtime/configuration`載入並覆蓋，維持production Environment只有AWS／SSM控制面設定。

SSM 不會在 archive 驗證前執行 path-writing extract。它先驗證外部 SHA、以 stdlib 結構檢查
安全取得 archive 內唯一 validator、完成 allowlist/manifest 驗證，再安全 materialize 至新的
root-owned immutable release directory；SSM preflight 不會改寫 active Compose、runtime helper 或 Nginx
paths。staging canary/deploy/provider helper、catalog、Compose 與 Nginx render scripts 都從該 release root
執行，僅在 deployment health 成功後更新 current pointer。acceptance record同時綁定 validator SHA，host在執行
archive 內 validator 前會比對這個獨立 trust anchor。accepted S3 objects 在 bucket policy 層要求 `If-None-Match: *`，
且 bucket keys 關閉以保留 unit-prefix KMS encryption context。

FinDB v2的Nginx runtime catalog只接受
`/opt/findb/releases/<bundle-sha256>-<run-id>-<attempt>/infra/deploy/runtime-secrets/findb.json`；
production同樣只走此v2 release identity。staging在recycle horizon內另保留尾碼`-findb`的v1 accepted
release identity唯讀replay相容性，production不得使用。既有bootstrap的
`/opt/findb/runtime-secrets/findb.json`固定路徑仍受catalog metadata、target、account與consumer驗證，不能用來
放行其他release路徑。

以下是Phase 3當時建立的accepted record；`b499869c8ff86e09232c1b55516787ae7ed5d2f0`已不是目前
FinDB或Fetcher accepted identity：

- FinDB normal run [33260508254](https://github.com/FPI-TW/findb/actions/runs/33260508254)建立
  `findb/accepted/b499869c8ff86e09232c1b55516787ae7ed5d2f0/0f90cbe570e34bc8bebe5c9a1737edfd881a7bc4e9d83ca4103a79dd884986cf.tar`；
  replay run [33293482050](https://github.com/FPI-TW/findb/actions/runs/33293482050)重用相同backend／Dashboard
  digests，accepted tar／record VersionId保持不變，並切換`/opt/findb/current`至本次immutable release。
- Fetcher normal run [33261139792](https://github.com/FPI-TW/findb/actions/runs/33261139792)建立
  `fetcher/accepted/b499869c8ff86e09232c1b55516787ae7ed5d2f0/7513a17912111e0a1f5f34fa344ba5847a26eb64cf66e976d993273c75b1af15.tar`；
  replay run [33294103564](https://github.com/FPI-TW/findb/actions/runs/33294103564)重用相同Twelve Data／FinLab／Shioaji
  digests，accepted tar／record VersionId保持不變，並切換`/opt/fetcher/current`至本次immutable release。
- Replay的bundle generation／upload／persist steps均依契約skipped，production jobs亦skipped。Fetcher
  replay的FinLab acquisition smoke同樣skipped，不得把它當成Phase 2A原生provider cycle或資料取得驗收。

### FinDB Phase 4 SSM live record（2026-08-30）

Phase 4驗收當時的FinDB accepted commit為`0a45328539cc66eff8e1b63afc6ac3b064b406e8`。

FinDB normal runs [33303655357](https://github.com/FPI-TW/findb/actions/runs/33303655357) 與
[33304135877](https://github.com/FPI-TW/findb/actions/runs/33304135877)均以OIDC＋SSM完成；兩次使用
相同image digests。accepted replay run
[33304630225](https://github.com/FPI-TW/findb/actions/runs/33304630225)使用
`findb/accepted/0a45328539cc66eff8e1b63afc6ac3b064b406e8/9c61a81fb351ae7ed17a0cc7bf88e8b5804a3a4aa481b82ec801a7a334117311.tar`，
只執行preflight與activation，candidate與persist均依契約skipped。activation SSM command
`3899bcd3-32a9-444c-8402-5ce6ae30ac50`為`Success`／exit 0且有success marker；health回應為200。
accepted tar／record的VersionId仍為`.rTPHJxsyKs0cxDGa.EQtVObbtd8A6M6`／
`Loij0QSs05Iz.uCPojZV93w7Cl42JgqP`，沒有新增version或delete marker。

這些結果驗證immutable accepted replay與two-phase SSM protocol，**不**構成不同digest的previous-release
rollback rehearsal：兩次normal deployment的digest相同。在2026-08-30 Phase 4驗收當下，唯一舊的不同
digest accepted bundle（commit `b499869c8ff86e09232c1b55516787ae7ed5d2f0`）與當時deploy contract不相容，
也沒有不同Alembic revision的accepted release可安全進行live schema-incompatibility rejection。Owner已將這兩項
rehearsal移至Phase 6，因此不再阻塞Phase 4，但不得把同digest replay誤稱為rollback或宣稱演練已完成。
`staging-findb`的
`FINDB_EC2_HOST`、`FINDB_EC2_USER`、`FINDB_EC2_SSH_KEY`已在Phase 4 live acceptance後移除；其餘SSH
recovery／network或其他unit scope不在本次變更內。

### Fetcher Phase 5 SSM live record（2026-08-31）

PR [#222](https://github.com/FPI-TW/findb/pull/222)合併為
`c4c827a96c538d8be65787dcff0cb3492506098f`後，Fetcher normal runs
[33368243527](https://github.com/FPI-TW/findb/actions/runs/33368243527)與
[33369135142](https://github.com/FPI-TW/findb/actions/runs/33369135142)均以OIDC＋SSM完成
preflight、candidate、accepted-record與activation。Accepted replay加FinLab acquisition smoke run
[33372826303](https://github.com/FPI-TW/findb/actions/runs/33372826303)亦成功；replay只使用既有accepted
identity，FinLab smoke由獨立bounded SSM command執行。

Deployment correctness不再依賴CloudWatch log stream或hard-coded plugin ID。Workflow在
`get-command-invocation`回傳`Success`後，直接從`StandardOutputContent`逐行精確比對本次command的
success marker；`Status`、`StatusDetails`、`ResponseCode`、command ID及instance ID任一不符即fail closed。
CloudWatch仍作為完整、durable的stderr與command audit，但不是deployment correctness的唯一signal。上述兩次normal
deployment與replay／smoke皆通過這個inline marker protocol。

FinLab smoke SSM command `f1a93849-490e-431b-b3f5-185504566bbf`的`Status`、`StatusDetails`與
response code為`Success`／`Success`／`0`；`StandardOutputContent`只有精確的
`phase5_finlab_smoke_marker=33372826303-1-finlab-smoke status=success`。CloudWatch保持啟用，
但不參與這個correctness判定。

Live acceptance另確認：

- 三個provider維持單一stable writer、restart count 0，candidate／previous／preflight leftovers為空；
- provider consumer與calendar credential皆保持隔離，cross-provider read fail closed；staging FinLab
  container內的現行calendar client對未發布的TW 2099年度回應HTTP 404並fail closed，且未觸發provider；
- FinLab ingestion run `01a0572e-64d6-73a3-8d1c-0055c0eec96e`為`completed`，attempt 1取得並
  durable terminal delivery 2/2筆；normalization job為`completed`、outbox為`published`、policy為`pass`，
  FinLab feed freshness為`fresh`且沒有open missing-delivery alert；
- candidate失敗與中斷復原均保持`/opt/fetcher/current`不變、恢復原stable containers並清除
  candidate／previous／preflight leftovers；早期pre-activation失敗未執行persist或activation；
- Owner將Twelve Data、FinLab、Shioaji scheduler恢復並維持`running`，desired revisions分別為
  `10`／`10`／`8`；
- `staging-fetcher`的`FETCHER_EC2_HOST`、`FETCHER_EC2_USER`、`FETCHER_EC2_SSH_KEY`已刪除。

這組證據當時只完成Phase 5 transport與bounded FinLab acceptance，不能證明整體queue從未有歷史
`failed`、`retry_exhausted`或`missing`項目，也不能單獨證明四個active feeds的完整原生排程驗收。
四個feed的後續原生週期與多交易日pre/post package已分別於2026-09-09及2026-09-15完成；GitHub
Environment application runtime copies已於2026-09-09清理；
Phase 6後續已完成RDS restore、encrypted root replacement、Session Manager recovery、SSH ingress與host key退場、
custom alarm coverage、different-digest rollback、schema rejection、SQLite recovery及RabbitMQ DR。
Current-volume DLM policy與即時recovery snapshots亦已建立；首兩個不同雙volume排程週期均已通過，
且至2026-09-13已連續完成五個週期。

### Phase 6 live recovery record (2026-09-01 to 2026-09-03)

- `fin-db`維持private、KMS encryption、deletion protection與10-day automated backup／PITR。
  `2026-09-01 13:46:35 +08:00`由資料點`05:40:43Z`建立
  `fin-db-phase6-restore-20260901`；RDS於`13:55:53 +08:00`記錄restore完成，約9分18秒。
  SSM command `4f870a1d-6692-4f9c-8954-09d312d70e57`確認source／restore revision均為
  `d6e7f8a9b0c1`、instrument counts為`8/8`且connectivity pass；rehearsal instance於
  `14:22:55 +08:00`刪除。此結果證明一次PITR restore，不等同持續測試backup failure notification。
- FinDB與Fetcher current root volumes已分別替換為encrypted gp3
  `vol-071822e2fe38c3991`（50 GiB）及`vol-07a726b6215c34c20`（30 GiB），共用Phase 6
  EBS CMK。兩次post-replacement SSM validation均為Success／exit 0，FinDB application／RabbitMQ與
  Fetcher三個scheduler恢復運行。來源encrypted migration snapshots標記只保留至2026-09-08。
  PR [#240](https://github.com/FPI-TW/findb/pull/240)合併後，以DLM policy
  `policy-0d0a29c9e19f6323e`精確選取兩個current-volume tag，每日`09:00 UTC`建立snapshot並保留7份；
  policy建立當時為`ENABLED`。立即復原點`snap-0fd82febf04befa97`（FinDB）與
  `snap-03efabf42ff2b398c`（Fetcher）皆為`completed`、encrypted，保留至2026-10-03。
  這證明current-volume recovery point可建立，但不單獨證明DLM執行；後續狀態見下方
  2026-09-08 follow-up。
- 兩台managed node均為SSM Online。CloudTrail於2026-08-26記錄Tyler分別透過
  `SSM-SessionManagerRunShell-findb-staging`與`SSM-SessionManagerRunShell-fetcher-staging`成功建立
  Session Manager session；2026-09-01 command `08d25aff-4ff1-4c38-b2b0-0bc0f7c66f04`再次對兩台完成
  non-mutating recovery check。
- `sg-0194615fe18784889`、`sg-070694f093a25cb31`與`sg-0c98f59c6961a00d2`目前都沒有TCP/22
  ingress。2026-09-03 SSM commands `c7a6f1d1-f5ad-4fc1-b48f-a5010aa9c0f3`與
  `829d14fe-fd55-42d7-ab5b-696dc13ef1e5`從FinDB／Fetcher的`root`、`ubuntu`
  `authorized_keys`各移除一筆精確指紋匹配；獨立後驗commands
  `b7225864-e69a-422f-acfc-2fda0dd10858`與`b5a45195-9b99-462d-aa0e-ac919b2a694f`
  均為Success、target count為0。隨後以兩份custom Session Manager documents建立並正常退出
  sessions `Tyler-fbsz8svdkq55265uaugiu7jpqa`與`Tyler-gaivcgfjkusxozqi63liad5fni`；CloudTrail
  `StartSession` events為`e05d7c84-b202-4a58-933c-0b6c2fdbfdd5`與
  `aafd8f51-1f61-4269-90cf-887bff68e534`，且兩個session及四個移除／後驗commands的streams均已在
  各自`/findb/staging/{findb,fetcher}/ssm` log group回讀。最後刪除`fb-db-key`與
  `findb-fetcher-key`，
  `describe-key-pairs`回讀為空；CloudTrail `DeleteKeyPair` events分別為
  `a5fcad20-b003-43a0-a7e9-3ac57c1ff531`與`75a221d0-e6c0-4d01-afcb-7365fd0aa42e`。
  兩台managed nodes後驗仍為SSM Online。EC2 instance metadata可能繼續顯示建立instance時的歷史
  `KeyName`，不代表key-pair resource或host key仍存在。
- `2026-09-03 01:44:44Z`公開cache metadata顯示instrument cache由canonical Serve API重生為8筆、
  macro cache重生為0筆；兩者generated timestamp一致且cache仍不是durable source of truth。
- FinDB application-only rollback run
  [33734709446](https://github.com/FPI-TW/findb/actions/runs/33734709446)重播accepted commit
  `bd5cb5dceab10982a1578bc8e07df017a24b0359`，以不同backend/dashboard digests取代當時current
  `6a6b64cd12b631d421b365b01f50e9a422a5ea79`，未執行migration；SSM command
  `20a3a0a3-c06e-4fa3-807d-3f27b78b9835`確認兩張digest、全部container health/restart count及
  Alembic `a8b9c0d1e2f3`。其後run
  [33736955949](https://github.com/FPI-TW/findb/actions/runs/33736955949)成功恢復protected-main accepted
  bundle，current release為`aa3d86258f7e7c5ad2961e9b699412f13cc0dc54fd96081ab4d4239dbf1d640e-33736955949-1-findb`。
- 不同revision的accepted commit`0a45328539cc66eff8e1b63afc6ac3b064b406e8`先由run
  [33736392355](https://github.com/FPI-TW/findb/actions/runs/33736392355)在更前面的deployment-contract
  compatibility gate拒絕。再以正式`predeploy_db_check.py`的exact activation gate執行SSM command
  `4dd6baba-7427-4b59-a597-613409c35737`：current `a8b9c0d1e2f3`對target
  `d6e7f8a9b0c1`同時回報`schema_compatible_with_target=false`及`schema_exactly_at_target=false`，
  四個writers維持`exited`且current pointer未變，完成schema-incompatibility fail-closed演練。
- Fetcher SSM command `c76b98a7-61f6-4912-90a3-a235ebc47432`對三個live SQLite採online backup，
  再複製至隔離restore檔執行`integrity_check`及SHA-256比對；Twelve Data、FinLab、Shioaji的backup／restore
  size與table counts皆一致。檔案位於root-only的
  `/var/backups/findb-fetcher/phase6-20260903`；它仍與來源同在current root EBS，獨立off-host保護依賴後續
  DLM snapshot，不把本機副本單獨稱為host-loss backup。
- RabbitMQ SSM command `055aee8e-f2a6-4fdb-b43e-6232dfb11d48`在三個scheduler desired state停止、
  FinDB writers停止後，把舊broker目錄保留為
  `/var/lib/findb/rabbitmq.phase6-pre-rebuild-20260903T091531Z`，以不同inode的空目錄重建live broker。
  Version-controlled policy為`exited 0`，queue與DLQ重新宣告且深度皆0；worker heartbeat為3.7秒。
  PostgreSQL前後均為jobs `completed=171`、`failed=1`、unpublished outbox 0、expired leases 0、
  retry exhausted 1及missing deliveries 0，證明broker可由DB-authoritative state與版本控制topology重建。
  舊目錄暫留供復原稽核，確認不再需要後才能另行核准清理。

2026-09-08 follow-up：DLM policy `policy-0d0a29c9e19f6323e`的schedule-only tag已由重複的
`Purpose`改為`BackupPurpose`。Fresh saved plan SHA-256為
`49fb3fe75512a8d97388daf312b8d0f541bc9935aef48f7d150a36f0eb0dba46`，guard通過且只有一筆
in-place update；apply為`0 added, 1 changed, 0 destroyed`。AWS回讀policy為`ENABLED`、
`CopyTags=true`、每日`09:00 UTC`且保留7份。2026-09-10補查確認2026-09-09 09:41 UTC首個
scheduled cycle：`vol-07a726b6215c34c20`建立`snap-01ebe8336b7ced28c`，
`vol-071822e2fe38c3991`建立`snap-0396321f0243a9ed1`；兩者均`completed`、encrypted，並帶
`aws:dlm:lifecycle-policy-id=policy-0d0a29c9e19f6323e`、
`aws:dlm:lifecycle-schedule-name=DailyCurrentRootRecoveryPoints`、`dlm:managed=true`及正確
`BackupPurpose`。每個volume當時各1份，未超過policy count 7；manual snapshots未計入。
因`CopyTags=true`而複製的舊`Retention=retain-until-2026-09-08`只屬非權威來源標籤，DLM實際保存由
`RetainRule.Count=7`控制。2026-09-14回讀確認第二個不同週期已於2026-09-10完成：Fetcher
`snap-0576caba14540b2ac`與FinDB `snap-0df1a639ad70bd5fc`均為`completed`、encrypted，且帶相同
exact policy／schedule／managed／BackupPurpose tags。至2026-09-13已連續完成五個雙volume週期，
每個volume各5/7份，未超過retention count；manual snapshots持續排除於計數，因此recurring chain
執行驗收完成。
2026-09-09另以零刪除OpenTofu apply上線`DLMPolicyHealthy`與
`findb-staging-dlm-policy-unhealthy`，指定policy為healthy且43個staging alarms全為`OK`；此控制面
告警不能替代實際recovery-point驗收。

同日完成active-backlog fault acceptance：三個provider producers暫停後，以retained FinLab raw建立
一筆含兩列資料的rerun backlog，短暫撤銷RDS SG中FinDB EC2 SG到TCP/5432的單一規則；新啟動worker取得delivery
並呈現`unacked=1`後被SIGKILL，message回到`ready=1`。DB規則立即以相同source／port／description
恢復，worker恢復原`unless-stopped`後完成2/2 canonical upsert，queue／DLQ、unpublished outbox及
expired leases皆為0，兩筆canonical lineage均指向單一completed rerun。固定idempotency另驗證相同
內容`202`重用原run、同key不同內容回`409/IDEMPOTENCY_PAYLOAD_MISMATCH`，raw count不變。

2026-09-09另以目前repo revision集中重跑P0故障與時間邊界驗收：Fetcher的client retry、scheduler
state／control、schedule／calendar preflight及Shioaji scheduler共164項全數通過；Backend的delivery
monitor、delivery policy及normalization queue共65項全數通過。這229項測試分別覆蓋bounded retry、
expired lease reclaim、graceful stop、holiday、台北固定操作時段跨美國DST，以及late delivery解除
missing alert。它們與上述live worker-kill／RabbitMQ redelivery證據共同關閉該P0項目，但不取代四個
active feeds的多交易日config／universe／credits／pre-post live evidence package。

同日以SSM command `d6225912-98bd-4d98-87e6-5eecdc71bcde`讀取live Admin freshness：FinLab、
兩個Shioaji minute feeds及Twelve Data均為`ready`／`fresh`，expected與coverage data date皆為
2026-09-08，且沒有open missing alert。Command `cf7b2818-4ebd-4022-a3aa-6e7f550e1219`另行回讀
DB-authoritative delivery policies：Twelve Data每symbol最低1筆、FinLab source override最低2筆；
兩個minute feeds採sequenced snapshot，record-count policy維持disabled。兩份command output均寫入
KMS-encrypted `/findb/staging/findb/ssm`。該次資料支持既有bounded門檻，但當時尚無真實provider欄位
消失或異常空snapshot樣本可校準升級，因此policy維持`warn`，當時不得宣稱該P0項目已關閉；同日
後續受控異常校準與alarm自然恢復已補齊並關閉此項，詳見監控runbook。

2026-09-09的Twelve Data原生schedule補齊accepted-SHA四feed gate：AAPL、MSFT、NVDA均在attempt 1
完成，checkpoint由2026-09-04推進到2026-09-08，使用3／3 credits；三個Raw R2 checksum、三個Source
`202`、completed run/job、published outbox、DQ error 0、canonical lineage及公開Serve／Dashboard查詢均通過。
FinLab及兩個Shioaji feeds已有同一accepted record後的原生成功證據，因此此單次完整原生週期門檻關閉；
多交易日完整evidence package當時仍依資料面backlog追蹤。

2026-09-15以2026-09-09 immutable `pre` manifest SHA-256
`7c234156bdf18964c7f1dc5d164a208a00c42db7458ee35fb30fffc0ec0e8e68`執行自然`post`驗收。FinLab、
兩個Shioaji feeds與Twelve Data均前進至2026-09-11及2026-09-14交易日，reviewed config／universe
identity未變；四feed的Source／Raw／job／outbox／DQ／canonical、多交易日與持久化Source `202`均通過，
minute Serve邊界維持`not_applicable/market_minute_read_model_not_exposed`。Post manifest SHA-256為
`36489b45ad94e5c10f4037f931898ca1b0ae8474f657395722124adaa5e50a24`，以KMS保存於versioned S3 key
`evidence/staging/active-feeds/2026-09-15/post-36489b45ad94e5c1.json`，version
`.quZPhy3PoNJRFp3l5qzp14y7OCpeV7Z`；因此四feed多交易日pre/post P0 gate已關閉。

同日RabbitMQ唯讀健康commands `36989907-c2d4-4e99-a351-f43c6bd91788`與
`3dc62594-d707-46b9-a746-d89ea6ed475b`確認broker自2026-09-03起連續運行且healthy，restart／OOM／
local alarms為0，兩個durable queues與DLQ均為0，worker ping成功；runtime-secret wrapper內的
DB-authoritative health為`unpublished_outbox=0`、`expired_leases=0`、`missing_deliveries=0`、heartbeat
age 7.8秒。Live與舊稽核目錄inode不同，舊副本為40 MiB；FinDB root DLM仍`ENABLED`且已有6/7份
completed、encrypted recovery points。取得明確授權後，SSM command
`57e6a9e8-87ff-41c9-83f6-cc4a8a0cd99c`先確認舊目錄不是symlink且與live inode不同，再只刪除
`/var/lib/findb/rabbitmq.phase6-pre-rebuild-20260903T091531Z`。刪除後舊路徑不存在、live path保留，
RabbitMQ ping成功；DB-authoritative queue health刪除前後皆為queue／DLQ、unpublished outbox、expired
leases及missing deliveries 0，command為`Success`／exit 0且stderr空白。

同日依使用者明確授權移除`staging-findb`13枚與`staging-fetcher`10枚舊application runtime secrets；兩個
Environment secret清單刪後均為空。SSM commands `fb74877d-3829-4260-9d4b-4465cb2f6f4d`及
`84bb294b-91d8-46b3-ac70-71e091e0629e`確認FinDB完整canary與Fetcher三個provider catalogs仍可由各自
instance role從Secrets Manager載入，且check-only輸出均已移除。Queue unpublished outbox、expired leases及
missing deliveries皆為0，三個scheduler fresh／ready，public health與Dashboard為HTTP 200，兩台SSM Online，
43個alarms為43 OK。刪除僅影響GitHub staging Environment copies，不影響AWS Secrets Manager或production。

2026-09-15由乾淨protected `main` merge SHA
`5a7f6c74ab8e972b9a798026a9f37a3c7523b18b`完成TLS certificate expiry monitoring與Fetcher
運行中container security自動反查的live apply。Fresh saved plan為4 add、5 in-place change、0 destroy
且無replacement；apply為4 added、4 changed、0 destroyed。兩個metric publisher associations version 5
成功；兩個後續SSM commands亦為`Success`／exit 0／stderr空白。相鄰兩個五分鐘bucket的TLS剩餘
天數為`71.33820040579862`與`71.33503951233797`，三個runtime-security metrics兩輪均為`1`，
新增四個alarms及全體50個alarms後驗皆為`OK`。更廣泛的queue-depth trend與market freshness policy仍另案；SSH入口、
長效recovery key、custom alarm、SQLite/RabbitMQ DR及rollback/schema演練均已有live evidence，不再列為
未完成風險。

Deploy後至少完成：

- **Workflow**：container與restart count、process health、Alembic head、RDS connectivity、
  RabbitMQ topology、worker ping及DB-authoritative queue health。
- **Operator acceptance**：public TLS／routing、bounded API smoke、terminal state與lineage。
- **Operator acceptance**：Fetcher另核對heartbeat、desired／observed state及terminal
  delivery。

失敗時：

1. 暫停provider並停止所有writers，保留raw、run、job、outbox、volumes與logs。
2. 收集bounded diagnostics，不輸出secret。
3. 只有schema相容時才能恢復previous image。
4. Schema已改且舊image不相容時保持writers停止，部署包含目前migration chain的forward fix。

禁止使用`docker compose down -v`、刪資料、`stamp head`、即席production downgrade或R2
mass delete強迫恢復。Serve健康且schema相容時可維持唯讀服務。
