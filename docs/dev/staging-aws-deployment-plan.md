# Staging AWS Deployment Completion Plan

> 狀態：待執行。本文是 staging AWS 控制面、部署身分與驗收的完成計畫；現行可操作
> runbook 仍以 [`../operations/deployment.md`](../operations/deployment.md) 為準。
>
> 最後盤點：2026-08-20。AWS、GitHub Environment 與 Cloudflare 的實際資源狀態未由
> repo 自動驗證，表中標示 `待盤點` 的欄位不得視為已完成。

## 目標與範圍

把目前可運作但仍依賴 SSH 與 GitHub Environment runtime secrets 的 staging 部署，收斂為：

```text
protected main
  -> reusable CI for the same commit
  -> build images once and record exact digests
  -> GitHub Environment: staging-findb / staging-fetcher
  -> GitHub OIDC
  -> service-specific AWS deploy role
  -> SSM Run Command to a tagged EC2 target
  -> EC2 instance role reads service-specific runtime secrets
  -> FinDB EC2 -> private RDS + Canonical R2
  -> Fetcher EC2 -> FinDB HTTPS + Raw R2
```

本計畫包含 staging 的 GitHub protection、AWS IAM、SSM、EC2、RDS、runtime secrets、
release manifest、migration、監控、備份與 recovery rehearsal。它不建立 production
資源、不擴大 active feed universe、不實作 Canonical R2 runtime，也不把單台 EC2 宣稱為
高可用。

## Repo 可證實的現況

| 項目 | 現況 | 完成 staging 仍缺少 |
| --- | --- | --- |
| Release units | FinDB 與 Fetcher 已有獨立 CI/CD、Environment 與 concurrency group | 將 AWS target、role、secret path 與 acceptance 寫入可稽核清冊 |
| CI gate | CD 以 `workflow_call` 執行同一 revision 的 CI | 舊 revision migration fixture、image runtime security 與 deploy bundle deterministic check |
| Image identity | 所有 runtime 都發布 commit SHA tag；FinDB 另發布 `latest` | 保存 registry 回傳 digest、以 digest 部署、禁止 Compose fallback 到 `latest` |
| EC2 transport | `appleboy/ssh-action` 與 `scp-action` 已固定完整 Action SHA | OIDC、service-specific deploy role、SSM、target tag restriction 與 command log |
| Runtime secrets | GitHub Environment secrets逐一傳到遠端 shell／container | AWS Secrets Manager 或 SSM SecureString、instance role、輪替與 GitHub secret 退場 |
| RDS rollout | 有 predeploy DB check、writer pause、單一 Alembic upgrade 與 revision check | RDS resource/backup inventory、migration credential分權、restore rehearsal與release紀錄 |
| Queue | RabbitMQ 在 FinDB EC2，以 EBS path保存；PostgreSQL是durable truth | volume backup/容量告警、broker全毀重建演練與明確RTO |
| Fetcher state | 三個provider runtime隔離，SQLite state與raw bucket binding有preflight | EBS/SQLite一致性備份、SSM rollout與完整排程週期觀察 |
| R2 | Raw與Canonical bucket/credential契約已拆分 | 外部bucket policy/lifecycle盤點；Canonical runtime尚不能作put/get acceptance |
| Protection | workflow固定第三方Action SHA，repo要求PR | CODEOWNERS、Environment protection與branch policy的外部驗證紀錄 |
| Observability | 應用內已有health、queue與freshness checks | CloudWatch agent/log retention、EC2/RDS/EBS alarms與告警接收者 |

目前 `.github/workflows/findb-cd.yml`、`.github/workflows/fetcher-cd.yml` 和
`docker-compose.prod.yml` 是現行行為的 source of truth。本文不把尚未查證的 AWS console
設定當作事實。

## Workflow / path matrix

| Unit | CI source paths | 自動CD paths | Images | Environment | Concurrency |
| --- | --- | --- | --- | --- | --- |
| FinDB | `backend/**`、`dashboard/**`、`contracts/**`、兩份Compose、`infra/nginx/**`、workspace lock/config與FinDB workflows | `backend/**`、`dashboard/**`、`infra/nginx/**`、`docker-compose.prod.yml`、workspace lock/config與`findb-cd.yml` | `ghcr.io/fpi-tw/findb`、`ghcr.io/fpi-tw/findb-dashboard` | `staging-findb` | `staging-findb`，`cancel-in-progress: false` |
| Fetcher | `fetcher/**`、`contracts/**`、contract generation所依賴的backend source/lock與Fetcher workflows | `fetcher/**`、`fetcher-cd.yml` | `ghcr.io/fpi-tw/findb-fetcher`、`findb-fetcher-finlab`、`findb-fetcher-shioaji` | `staging-fetcher` | `staging-fetcher`，`cancel-in-progress: false` |

兩個CD都由protected `main`的path-filtered push自動進入staging，也可手動dispatch；CD必須直接
呼叫同revision CI。Contract-only變更會觸發兩個CI但不自動部署Fetcher，仍依現行
backend-first runbook手動發起受影響的CD。後續新增deploy bundle、IAM/IaC或release manifest
路徑時，必須在同一個PR更新此矩陣與對應workflow filters。

## Release unit 與權限矩陣

| Unit | Images / services | RDS | R2 | EC2 state | Health / terminal acceptance | Deploy order |
| --- | --- | --- | --- | --- | --- | --- |
| FinDB | backend image：`serve`、`ingest`、`dispatcher`、`worker`、`raw-cleanup`；Dashboard image；RabbitMQ、nginx | `serve` read；`ingest`/`dispatcher`/`worker`/`raw-cleanup` write；one-shot migration migrate | `serve` Canonical read；`worker` Canonical read/write；其餘 none | RabbitMQ EBS、generated static cache | process health、public readiness、Alembic head、queue topology、worker ping、DB queue health、bounded API smoke | 先部署，contract變更時保持backend-first |
| Fetcher | generic、FinLab、Shioaji images；三個provider scheduler | none | Raw read/write；provider依共同object contract隔離 | provider-specific SQLite/checkpoint/cache EBS paths | scheduler preflight、single-writer、desired state convergence、heartbeat、bounded delivery terminal state | FinDB acceptance後部署 |

FinDB 仍是一個 deployment unit，因為 backend migration、Dashboard、nginx 與 Compose 必須以
同一份 manifest 驗證；backend 與 Dashboard image 必須各自記錄 digest。Fetcher 的三個 image
同屬一個 deployment unit，但 release manifest 必須列出三個 digest，不能只以共同 SHA tag
代替。

## Staging 資源清冊

Phase 0 必須將下表填入 target-specific、非敏感的實際識別資料。Secret 只記錄 ARN/name，
不得貼值、hash 或可比較片段。清冊完成前禁止停用現行 SSH recovery path。

| 類別 | `staging-findb` | `staging-fetcher` | 驗收要求 |
| --- | --- | --- | --- |
| AWS account ID | 待盤點 | 待盤點；原則上與FinDB相同 | workflow明確檢查STS account，不接受任意account |
| Region | 待盤點 | 待盤點 | GitHub Environment variable與主機region一致 |
| VPC / subnet | 待盤點 | 待盤點 | RDS private；若EC2暫留public subnet須列例外與退場條件 |
| EC2 instance ID / tag | 待盤點 | 待盤點 | `Project=FinDB`、`Environment=staging`、`DeploymentUnit=findb|fetcher` |
| EC2 instance profile | `FinDBStagingInstanceRole`（待建立/查證） | `FetcherStagingInstanceRole`（待建立/查證） | 只讀自己的secret path與deploy bundle；可寫自己的log/metrics |
| GitHub deploy role | `GitHubDeployFinDBStagingRole`（待建立） | `GitHubDeployFetcherStagingRole`（待建立） | OIDC subject綁定對應Environment；SSM resource以target tag限制 |
| SSM managed node | 待盤點 | 待盤點 | Online、無需public SSH、command output送CloudWatch Logs |
| RDS / Aurora | instance/cluster ARN、engine/version、DB subnet group待盤點 | 無權限、無網路路由 | `PubliclyAccessible=false`；只允許FinDB EC2 SG；backup/PITR啟用 |
| RDS security group | 待盤點 | 不得被允許 | ingress只使用SG reference，不使用公開CIDR |
| KMS key | runtime secrets key待決定 | 使用不同key或至少不同key policy scope | deploy role不可解密runtime secret；instance role只解密自身路徑 |
| Secret prefix | `findb/staging/findb/` | `findb/staging/fetcher/` | 不共用RDS、R2、provider、Source/Admin或deploy credential |
| Deploy artifact | 待決定：專用private S3 deploy bucket或OCI artifact | 同機制、獨立prefix | exact SHA path、checksum、immutable/versioned；不得混入application R2 bucket |
| CloudWatch | log groups、alarms與通知target待盤點 | log groups、alarms與通知target待盤點 | retention、owner與alarm test都有紀錄 |
| Backup | RDS、RabbitMQ EBS、static volume policy待盤點 | SQLite/checkpoint EBS policy待盤點 | restore rehearsal成功且RTO/RPO有量測 |
| Public endpoint | `FINDB_PUBLIC_HOST`對應DNS/TLS待查證 | 無public inbound endpoint | 外部HTTPS readiness、certificate expiry與DNS監控 |

### 已知的 staging 例外

- FinDB 與 Fetcher 各只有單一 EC2 target，是共同故障點；staging 不承諾 HA。升級條件是
  staging 被用作長時間 SLA 驗證、單機故障阻塞 release，或 production topology 要求先在
  staging 演練多主機 rollout。
- RabbitMQ 與 Fetcher SQLite 使用 EC2 attached storage。RabbitMQ 可由 PostgreSQL outbox
  重建；Fetcher SQLite包含retry/checkpoint，必須有一致性備份與單一writer recovery。
- 若目前 EC2 位於 public subnet，可在完成 SSM 且關閉 SSH ingress後暫時保留；移往private
  subnet／ALB的owner與期限需在Phase 0填入，不能把public EC2視為最終網路架構。
- Canonical R2程式路徑尚未實作，因此 staging AWS completion 不以Canonical put/get為
  blocker；但credential隔離、private bucket與「不可宣稱已驗證」仍是必要條件。

## 身分與 secret 設計

### GitHub OIDC trust

兩個 deploy role 的trust policy只接受GitHub OIDC provider，並同時限制：

```text
repository: FPI-TW/findb
aud: sts.amazonaws.com
sub (FinDB):  repo:FPI-TW/findb:environment:staging-findb
sub (Fetcher): repo:FPI-TW/findb:environment:staging-fetcher
```

實作前要以實際OIDC claim確認owner大小寫；trust policy不可退化成整個organization、任意
branch或任意Environment。只有deploy job取得`id-token: write`，CI與build job不得取得AWS
身分。

Deploy role只需要：

- 驗證自身STS identity；
- 對對應tag的EC2 managed node執行指定SSM document／Run Command並讀取結果；
- 寫入該unit的versioned deploy bundle與release manifest（採S3方案時）；
- 讀取必要的EC2/SSM部署狀態。

Deploy role不得讀取Secrets Manager secret value、RDS data、R2 credential或另一個unit的
SSM target。EC2 instance role負責讀取runtime secret與deploy bundle，兩種角色不得重用。

### Secret catalog

預設使用Secrets Manager保存RDS、provider、R2、DB-backed API key、RabbitMQ與GHCR pull
credential；非敏感且低頻變更的設定留在GitHub Environment variables或SSM Parameter Store。
若選用SecureString，必須使用target-specific KMS key/policy，不能改用普通String。

| Secret group | 建議ID/prefix | 唯一consumer |
| --- | --- | --- |
| RDS application | `findb/staging/findb/rds-application` | serve、ingest、dispatcher、worker、raw-cleanup所需的最小DB角色 |
| RDS migration | `findb/staging/findb/rds-migration` | one-shot migration/preflight；不得常駐注入application containers |
| RabbitMQ | `findb/staging/findb/rabbitmq` | RabbitMQ與需要broker URL的FinDB services |
| Admin / Serve keys | `findb/staging/findb/application-keys` | 依現行Compose逐一注入；break-glass不得供一般health使用 |
| Canonical R2 publisher | `findb/staging/findb/r2-canonical-publisher` | worker |
| Canonical R2 reader | `findb/staging/findb/r2-canonical-reader` | serve |
| Fetcher shared | `findb/staging/fetcher/shared` | calendar Serve key與非provider共用runtime |
| Provider credentials | `findb/staging/fetcher/{twelve-data|finlab|shioaji}` | 對應provider container |
| Raw R2 | `findb/staging/fetcher/r2-raw` | 需要raw persistence的Fetcher runtime |
| GHCR pull | `findb/staging/{findb|fetcher}/ghcr-read` | 主機deploy helper；只允許read package |

若GHCR packages可安全改為public，優先使用anonymous digest pull並移除GHCR secret；若維持
private，package-read token視為第三方長效runtime secret保存與輪替，不能再由workflow把
`GITHUB_TOKEN`轉送到主機。

第一階段可由受控deploy helper將secrets寫入`tmpfs`上的`0600` env file，再只注入需要的
container；不得echo、寫入持久`.env`、放在SSM command參數或診斷輸出。後續若要降低
`docker inspect`可見性，再讓application直接以AWS SDK讀secret或改用container secret
mechanism。這項過渡風險必須寫入release acceptance。

## 不可變 release 與 promotion

每個CD run只build一次，並產生一份versioned release manifest：

```json
{
  "schema_version": 1,
  "deployment_target": "staging",
  "deployment_unit": "findb-or-fetcher",
  "commit_sha": "40-char SHA",
  "images": {"name": "registry/repository@sha256:..."},
  "migration_revision": "alembic revision or none",
  "contract_versions": ["versioned contracts"],
  "deployment_bundle_sha256": "...",
  "created_by_run_id": "..."
}
```

- Build jobs必須從`docker/build-push-action`取得digest output；SHA tag只作索引。
- `docker-compose.prod.yml`改收完整`image@sha256:...`，移除`latest`與缺值fallback。
- Deploy前核對manifest commit、CI revision、image digest、bundle checksum與target。
- Host在停止任何writer前先pull exact digest並render `docker compose config`。
- 成功後保存accepted manifest、Alembic revision、時間、SSM command ID與acceptance結果；
  previous accepted manifest是唯一application rollback候選。
- 未來production promotion只能使用staging已接受的相同digests，不能重新build；production
  尚未建立期間，先確保manifest格式支援此查驗。

Deploy bundle建議使用獨立、private、versioned的AWS S3 control-plane bucket，讓GitHub OIDC
role上傳、instance role唯讀exact key，並啟用versioning與retention。這不是application object
storage，也不得替代Raw/Canonical R2；若團隊選OCI artifact，必須提供同等checksum、immutability
與instance pull權限後再實作。

## 實作波次

每一波各自提PR；先完成read-only或dual-path驗證，再移除舊路徑。不得在同一個deployment
同時切換OIDC、secret來源、image identity與migration流程。

### Phase 0：盤點與變更保護

- [ ] 填完staging資源清冊，記錄owner、AWS account/region、resource ARN/ID、資料分類、
  backup policy、告警接收者與公開網路例外。
- [ ] 驗證`staging-findb`、`staging-fetcher`只允許protected `main`，開啟required reviewer與
  prevent self-review（方案支援時）。
- [ ] 建立`.github/CODEOWNERS`，至少覆蓋workflows、`infra/**`、Compose、migrations與contracts。
- [ ] 決定IaC工具、deploy bundle transport與secret store；AWS console現況先import或以
  read-only data source記錄，禁止平行建立第二套同名資源。
- [ ] 記錄目前accepted SHA、Alembic revision、running containers、RDS snapshot與SSH recovery
  owner，作為後續變更基線。

Exit gate：清冊無未知的target、database、secret owner或backup owner；目前SSH部署仍可用。

### Phase 1：OIDC、SSM與instance role基礎

- [ ] 建立GitHub OIDC provider與兩個staging deploy roles；trust綁定各自Environment subject。
- [ ] 為兩台EC2建立獨立instance profile、必要target tags、SSM agent與Session Manager設定。
- [ ] SSM command output送到unit-specific CloudWatch log group，設定retention且禁止secret輸出。
- [ ] 以OIDC執行無副作用preflight：STS account/role、target count恰為1、environment marker、
  Docker/Compose版本、disk/inode、time sync、DNS與instance profile。
- [ ] 收斂security group：SSM確認穩定前保留受限SSH recovery；穩定後移除TCP/22 ingress。

Exit gate：兩個Environment只能命中各自一台EC2；cross-unit SSM與secret read均被IAM拒絕。

### Phase 2：Runtime secrets遷移

- [ ] 依secret catalog建立target-specific secrets與KMS policy；先寫入新版本，不刪GitHub值。
- [ ] 新增host-side secret loader，以allowlist取值、寫入tmpfs、驗證owner/mode，結束後清理。
- [ ] 用現行SSH CD選擇一次非資料產生的staging deploy，改從instance role取secret，確認container
  只取得自身需要的credential。
- [ ] 逐一輪替DB-backed keys、R2/provider/RabbitMQ/GHCR credentials，觀察一個完整排程週期。
- [ ] 確認last-used與health後，從GitHub Environment及local `.env.remote`契約移除runtime secret；
  GitHub只保留AWS role ARN、region、target selector、public host、secret identifier等非敏感值。

Exit gate：deploy role無法讀secret value，workflow log與SSM command不含runtime secret，舊
GitHub runtime secret已撤銷而非只複製。

### Phase 3：Digest與release manifest

- [ ] Build jobs輸出每個image digest並建立manifest；FinDB記錄backend+Dashboard，Fetcher記錄
  generic+FinLab+Shioaji。
- [ ] 建立versioned deploy bundle，納入Compose、nginx templates與deploy helper checksum。
- [ ] Compose改為必填完整image reference，CI驗證缺值fail closed且不存在`:latest`。
- [ ] CI驗證manifest schema、SHA/digest格式、bundle checksum與deterministic generation。
- [ ] 在不停止服務的情況下，由SSM target pull並inspect所有exact digests。

Exit gate：相同manifest可重播且不重新build；任一tag漂移不影響部署內容。

### Phase 4：FinDB改走SSM

- [ ] 將FinDB deploy job改成OIDC＋SSM；移除SSH/SCP action與`FINDB_EC2_*` secrets。
- [ ] Preflight確認RDS TLS、revision、connection headroom、long transaction、backup/PITR與磁碟。
- [ ] 先停`ingest`、`dispatcher`、`worker`、`raw-cleanup`及所有其他DB writer，再以migration
  credential執行單一Alembic job；`serve`僅在schema相容時保留。
- [ ] 啟動candidate後驗證container、internal health、public TLS/readiness、queue topology、
  worker ping、DB queue health與bounded DB transaction。
- [ ] 保存accepted manifest與SSM command ID；演練application-only previous digest rollback，並
  驗證schema不相容時拒絕回切、writers保持停止。

Exit gate：連續兩次FinDB staging deploy與一次rollback rehearsal不使用SSH；migration失敗不會
啟動新writers。

### Phase 5：Fetcher改走SSM

- [ ] 將Fetcher deploy與FinLab smoke改成OIDC＋SSM；移除`FETCHER_EC2_*` secrets與workflow
  傳送的provider/R2/Source credentials。
- [ ] 保留provider-specific state owner/mode、SQLite quick-check、raw bucket marker與single-writer
  reconciliation全部現行gate。
- [ ] 以DB desired state=`stopped`部署三個exact digests；preflight通過後逐一恢復核准的
  scheduler desired state。
- [ ] 驗證cross-provider secret isolation、calendar fail-closed、heartbeat與bounded terminal
  delivery；觀察至少一個完整排程週期。
- [ ] 演練candidate失敗、首次部署失敗與previous container recovery，不回復舊SQLite到新bucket。

Exit gate：連續兩次Fetcher staging deploy不使用SSH，三個provider維持單一writer且無credential
cross-read。

### Phase 6：監控、備份與SSH退場

- [ ] CloudWatch至少涵蓋EC2 status、disk/inode、memory、Docker restart、RabbitMQ disk/memory、
  scheduler heartbeat與deployment failure；RDS涵蓋CPU、free storage、connections、latency、
  deadlock及backup failure。
- [ ] 告警接入具名owner/on-call並演練一個synthetic alarm；文件記錄acknowledgement路徑。
- [ ] 驗證RDS encryption、automated backup、PITR、deletion protection與staging restore rehearsal；
  保存實測RPO/RTO。
- [ ] 對Fetcher SQLite使用停止writer或SQLite online backup API的一致性備份；對RabbitMQ EBS只作
  快速恢復輔助，仍演練由PostgreSQL outbox重建queue。
- [ ] 移除兩台EC2的SSH ingress、GitHub SSH secrets與未使用key pair；保留SSM break-glass流程並
  驗證CloudTrail／SSM audit。

Exit gate：SSM是唯一日常管理路徑，restore與broker rebuild均有最近一次成功紀錄，所有P0
部署項目可由evidence link驗收。

## 每次 cutover 的 go/no-go

只有全部為Yes才執行會停止writer的步驟：

- manifest commit與當次CI revision一致，所有image使用digest；
- target account、region、Environment與唯一EC2 tag match正確；
- previous accepted manifest與schema相容性已判定；
- RDS backup/PITR健康，migration lock/time與connection headroom在門檻內；
- Fetcher scheduler desired state與data caps已記錄，沒有未結束的provider cycle；
- RabbitMQ／Fetcher state volume空間正常，SSM command logs可用；
- on-call、觀察窗口、停止條件與forward-fix owner已確認。

No-go時不得用`docker compose down -v`、刪volume、`alembic downgrade`、`stamp head`、mass
delete或R2 object搬移來強迫部署。Schema已改且舊image不相容時保持writers停止，部署包含目前
migration chain的forward fix。

## 完成定義

Staging AWS deployment只有在以下全部有可查證evidence時才算完成：

- [ ] 四個GitHub workflow的path、CI、Environment、concurrency與release unit matrix一致。
- [ ] `staging-findb`與`staging-fetcher`有獨立OIDC deploy role、EC2 instance role與secret path。
- [ ] 日常deploy不使用SSH/SCP，EC2不開放SSH ingress，cross-unit IAM測試fail closed。
- [ ] 所有application image以digest部署，accepted release manifest可重播與promotion。
- [ ] Runtime secrets不在GitHub或persistent host env file，並已完成一次實際輪替。
- [ ] RDS private、backup/PITR/deletion protection與restore rehearsal有紀錄。
- [ ] Migration停止所有writers，失敗與schema-incompatible rollback路徑已演練。
- [ ] FinDB與Fetcher各完成兩次SSM deploy，並通過bounded acceptance與一個完整排程週期觀察。
- [ ] EC2/RDS/EBS/application告警、log retention、owner與recovery RTO/RPO已記錄。
- [ ] 所有staging例外都有owner、期限、補償控制與退場觸發條件。

## 尚待決策

| 決策 | 建議起點 | 必須在何時決定 |
| --- | --- | --- |
| AWS account / region / resource IDs | 沿用現有staging account與region，先import現況 | Phase 0 |
| IaC工具與state backend | 採團隊既有工具；若無，使用Terraform/OpenTofu＋encrypted remote state | Phase 0 |
| EC2 network | 先以SSM關閉SSH；private subnet＋ALB作後續目標 | Phase 0記錄例外，Phase 6確認期限 |
| Secret store | Secrets Manager保存runtime secrets；Parameter Store保存非敏感設定 | Phase 0 |
| Deploy bundle transport | 專用private/versioned S3 control-plane bucket；不用Raw/Canonical R2 | Phase 0 |
| GHCR pull | packages可公開則anonymous；否則AWS secret保存package-read-only token | Phase 2 |
| RDS credentials | migration與application角色分離；再依service拆read/write | Phase 2前 |
| Staging HA | 維持單EC2並明確不承諾HA | production規劃前重新評估 |
| Canonical R2 acceptance | credential邊界先完成，資料面等runtime實作後另案 | Canonical runtime PR |
