# Deployment Runbook

> 目前實際target是staging；production workflow capability存在，但production EC2與外部資源
> 尚未完成。Staging runtime secrets已由instance role從Secrets Manager載入，application image已由
> accepted bundle固定為exact digest；FinDB staging workflow 的程式碼已改為OIDC＋SSM two-phase bounded candidate／activation，
> 並已完成兩次正常live deployment與一次同accepted identity replay，Phase 4 exit gate已完成。不同digest的
> previous-release rollback與不同Alembic revision的schema拒絕演練移至Phase 6，仍未執行。
> `staging-findb`的三個FinDB deploy SSH secrets已刪除；Fetcher及production仍保留
> SSH相容路徑；退場計畫見
> [Staging AWS Deployment Completion Plan](../dev/staging-aws-deployment-plan.md)。

## Workflow與release units

| Workflow | Unit | Trigger | Environment／concurrency |
| --- | --- | --- | --- |
| `findb-ci.yml` | Backend、migration、Dashboard、contracts | PR由`required-ci.yml`路由；`workflow_call`、manual | 不讀deployment Environment |
| `findb-cd.yml` | Backend＋Dashboard | FinDB paths合入`main`時跑同revision CI／no-op bridge；manual才可rollout | `staging-findb`／`staging-findb` |
| `fetcher-ci.yml` | Fetcher、contracts、images | PR由`required-ci.yml`路由；`workflow_call`、manual | 不讀deployment Environment |
| `fetcher-cd.yml` | Generic＋FinLab＋Shioaji Fetcher | Fetcher paths合入`main`時跑同revision CI／no-op bridge；manual才可rollout | `staging-fetcher`／`staging-fetcher` |

Deployment concurrency一律`cancel-in-progress: false`。CD直接呼叫同revision reusable CI；
不可用branch最近一次成功取代。Contract-only變更會執行兩個CI，但不自動部署任一unit，
需要依backend-first順序manual dispatch。

FinDB deployment unit包含backend、Dashboard、nginx、RabbitMQ及Compose services。
Fetcher的三個provider images是另一個unit。production與pre-foundation legacy rollback仍以commit SHA
**tag**相容路徑部署；Phase 3 wave 1的staging path則在 build／reuse 後從 ECR exact SHA tag取得並嚴格驗證 digest，
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
`infra/deploy/release_manifest.py`，不使用 current checkout 的工具。materialize 後先以 raw bytes 驗證該
工具的 `protocol` 命令只輸出 `findb-release-manifest-v1` 加一個 LF（stdout/stderr、exit status 或任何額外
bytes 不符即 fail closed），再 generate／validate。Phase 3 foundation 之前、明確
`image_tag` 的 protected-main ancestor rollback 若 selected SHA 不含此工具，會發出 warning 並維持既有
SHA-tag rollback，但不產生 manifest artifact；這個 legacy 分支只適用 `ECR_REUSE_ONLY=true`。foundation
之後的 selected SHA 缺少或無法執行其工具時一律 fail closed；存在但沒有 v1 protocol 的檔案不是 legacy，
不會把其他 manifest error 降級為 legacy。
legacy rollback 的相容性前置檢查只比較實際同步到 staging host、或在 staging 前由 workflow 執行的
unit-scoped runtime inputs：FinDB 的 Compose、三個 staging-rendered nginx inputs（主設定、Source
allowlist、Cloudflare real-IP）、三個非敏感 renderer，以及 loader／command／host render／install／deploy／
catalog；Fetcher 的 loader／command／provider release／catalog。`serve-key.conf` 與
`render_nginx_serve_key.py` 不在 staging 前置路徑，故不在此集合。它刻意不比較純 CI 的 ECR build helper
或 release-manifest tool，因此這些 foundation-only 變更不會阻斷 pre-foundation SHA-tag rollback；任一
上述 host runtime contract 差異仍 fail closed。

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
2026-08-30完成normal deployment及replay live acceptance；production promotion仍未實作。

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

Workflow保留`staging`與`production` target及production manual dispatch能力；這只是預留的
workflow capability，不代表對應GitHub Environment或AWS資源已建立。現況如下：

| Target／resource | Current state |
| --- | --- |
| `staging-findb` GitHub Environment | 已建立，為目前FinDB staging target |
| `staging-fetcher` GitHub Environment | 已建立，為目前Fetcher staging target |
| `production-findb` GitHub Environment | N/A（尚未建立） |
| `production-fetcher` GitHub Environment | N/A（尚未建立） |
| Production FinDB EC2 | N/A（尚未建立） |
| Production FinDB RDS | N/A（尚未建立） |
| Production promotion snapshot／dump／seed | N/A（尚未建立） |

Push至`main`只執行same-revision CI及no-op bridge；staging rollout只接受protected `main`上的
manual dispatch。Production僅在上述Environment與target資源建立後允許manual dispatch。兩者都不配置GitHub Environment人工核准，仍以PR、required CI、protected
branch及target隔離控制變更。每個job只能取得自己unit與target的設定；branch與Environment
protection的實際外部設定需另行驗證。

完整變數與secret名稱不在本runbook重複維護，以
[infra/env契約](../../infra/env/README.md)、各target的`remote.env.example`、workflow及
`app.config.Settings`為準。Sync script只新增或更新GitHub Environment，不會刪除退休值；
operator必須另行移除不用的repository／Environment設定。

目前staging runtime secrets由EC2 instance role依consumer allowlist讀取Secrets Manager，host loader只在
`/run` tmpfs建立`0600` bundle並於使用後清理；GitHub Environment的舊runtime copies仍保留但不是
staging runtime source。GitHub OIDC與service-specific deploy role已用於AWS preflight／control-plane；
FinDB staging日常deployment transport已以兩次normal run與一次accepted replay完成SSM live驗證，
Phase 4 exit gate已完成；different-digest rollback與schema-incompatibility rejection移至Phase 6，仍未執行。
staging-findb已移除
`FINDB_EC2_HOST`、`FINDB_EC2_USER`、`FINDB_EC2_SSH_KEY`三個FinDB deploy secrets；此事不涵蓋TCP/22、
SSH recovery ingress／keys、Fetcher、production或GitHub runtime copies。
Fetcher與production仍使用SSH相容路徑。

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
  使用獨立deployment concurrency。
- **Operator pre-deploy**：確認Source經Cloudflare/nginx還原可信client IP後執行allowlist，
  TLS private key只存在FinDB target；外部Cloudflare、DNS與security group設定另行留證。
- **Operator pre-deploy**：確認repository政策與外部設定要求PR、required CI及protected
  branch。Staging rollout與production均只接受manual dispatch；staging必須選取protected `main`，兩者均不設
  GitHub Environment人工核准。

## Release順序

一般release：

1. **Workflow**：對同revision執行對應CI，build並push明確image identity，再pull candidate。
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
replay live acceptance已完成；FinDB日常host transport已在Phase 4切換為SSM，Fetcher仍使用SSH action。

SSM 不會在 archive 驗證前執行 path-writing extract。它先驗證外部 SHA、以 stdlib 結構檢查
安全取得 archive 內唯一 validator、完成 allowlist/manifest 驗證，再安全 materialize 至新的
root-owned immutable release directory；SSM preflight 不會改寫 active Compose、runtime helper 或 Nginx
paths。staging canary/deploy/provider helper、catalog、Compose 與 Nginx render scripts 都從該 release root
執行，僅在 deployment health 成功後更新 current pointer。acceptance record同時綁定 validator SHA，host在執行
archive 內 validator 前會比對這個獨立 trust anchor。accepted S3 objects 在 bucket policy 層要求 `If-None-Match: *`，
且 bucket keys 關閉以保留 unit-prefix KMS encryption context。

以下是Phase 3當時建立的accepted record；`b499869c8ff86e09232c1b55516787ae7ed5d2f0`不是目前
FinDB accepted identity。本文最後驗證的Fetcher accepted commit仍為該值：

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

目前FinDB accepted commit為`0a45328539cc66eff8e1b63afc6ac3b064b406e8`。

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
rollback rehearsal：兩次normal deployment的digest相同。唯一舊的不同digest accepted bundle（commit
`b499869c8ff86e09232c1b55516787ae7ed5d2f0`）與目前deploy contract不相容，workflow會fail closed；目前也
沒有不同Alembic revision的accepted release可安全進行live schema-incompatibility rejection。Owner已將這兩項
rehearsal移至Phase 6，因此不再阻塞Phase 4，但不得把同digest replay誤稱為rollback或宣稱演練已完成。
`staging-findb`的
`FINDB_EC2_HOST`、`FINDB_EC2_USER`、`FINDB_EC2_SSH_KEY`已在Phase 4 live acceptance後移除；其餘SSH
recovery／network或其他unit scope不在本次變更內。

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
