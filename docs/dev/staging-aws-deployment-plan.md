# Staging AWS Deployment Completion Plan

> 狀態：Phase 0–1 完成；下一個實作階段是 Phase 2 runtime secrets 遷移。本文是 staging AWS 控制面、部署身分與驗收的核心
> 成熟化計畫；現行可操作 runbook 仍以
> [`../operations/deployment.md`](../operations/deployment.md) 為準。
>
> 最後盤點：2026-08-27。Protected `main` 的 merge SHA
> `77212ce47b3138c2e21c6984e989b66239fd3cce` 已由兩個 GitHub Environments 完成 OIDC／SSM
> preflight及既有SSH deployment，OpenTofu remote state、OIDC/IAM、instance profiles、required
> tags、SSM managed nodes、Session Manager與unit-specific SSM logs均有live evidence。Phase 0與
> Phase 1已通過exit gate；「服務可用」仍不等同於Phase 2–6的secret遷移、digest／manifest、
> SSM deployment cutover、監控與災難復原已完成。Staging registry已決策改採Amazon ECR，但ECR
> repositories、publisher roles、workflow cutover、apply與live acceptance均為待建立／待驗收項目。

## 決策背景

本計畫依下列已確認前提收斂，不以完整企業級治理作為 staging 完成條件：

- staging 是 production 的前置驗證站，預期三個月內開始建立 production；
- staging 由小團隊共同維運，不要求正式 24/7 on-call；
- protected `main` 合併後維持自動部署 staging；production採unit-specific Git tag加手動
  artifact promotion，不另設Environment人工核准；
- 本輪採核心安全與可復原基線，不一次導入 HA、全面 IaC import 或完整企業稽核；
- IaC 只先管理新控制面資源，既有 EC2、RDS 與網路先盤點及引用，不因全面 import 阻塞 cutover。

因此，本計畫不是讓現有 staging 繼續運作的前置條件，而是讓 staging 能安全承擔 production
promotion、交接、重播與復原驗證的完成條件。

## 目標與範圍

把目前可運作但仍依賴 SSH、GitHub Environment runtime secrets 與 tag-only image identity 的
staging 部署，收斂為：

```text
protected main
  -> reusable CI for the same commit
  -> unit-specific OIDC publisher role
  -> build images once, push to staging ECR and record exact digests
  -> GitHub Environment: staging-findb / staging-fetcher
  -> GitHub OIDC
  -> service-specific AWS deploy role
  -> SSM Run Command to one tagged EC2 target
  -> EC2 instance role reads service-specific runtime secrets
  -> FinDB EC2 -> private RDS + Canonical R2 credential boundary
  -> Fetcher EC2 -> FinDB HTTPS + Raw R2
  -> accepted release manifest for later production promotion
```

本計畫包含：

- staging 的 GitHub protection、AWS IAM、SSM、runtime secrets 與 immutable release；
- FinDB migration、Fetcher single-writer、健康檢查與 bounded acceptance；
- 必要的 EC2、RDS、EBS、RabbitMQ 與 scheduler 監控及復原演練；
- 日常 SSH 退場與可稽核的 SSM recovery path。

本計畫不包含：

- 建立 production 資源或 production Environment；
- 實作production promotion workflow；本計畫只定義未來promotion必須遵守的tag與artifact契約；
- staging HA、Auto Scaling、多 EC2、blue/green 或 Multi-AZ 強制要求；
- 全面 import 既有 AWS 資源到 IaC；
- 擴大 active feed universe、production-scale backfill 或新增資料來源；
- Canonical R2 publish/read runtime；
- 正式 24/7 on-call、完整企業稽核或 staging 每次部署的人工核准。

## 各階段必要性

| 階段 | 判定 | 原因與調整 |
| --- | --- | --- |
| Phase 0：盤點與保護 | 必要 | 確認實際 target、資料與復原 owner；不要求全面 IaC import，也不對 staging 加Environment人工核准 |
| Phase 1：OIDC、SSM、instance role | 必要 | 建立service-specific AWS trust boundary與可稽核的SSM recovery，作為後續移除長效SSH deployment identity的前置條件 |
| Phase 2：Runtime secrets與ECR registry cutover | 必要 | 避免runtime secrets經GitHub runner與遠端shell傳遞；改由instance role取得ECR短效token，並完成credential rotation與GHCR credential退役 |
| Phase 3：Digest、manifest、CI gate | 必要 | staging 必須能重播並把相同 artifact promotion 到 production，不能依賴 `latest` 或可漂移 tag |
| Phase 4：FinDB SSM cutover | 必要 | 保留既有 migration 與 queue safety gate，只替換部署傳輸與secret來源 |
| Phase 5：Fetcher SSM cutover | 必要 | 保留 SQLite、Raw bucket binding 與 single-writer gate，只替換部署傳輸與secret來源 |
| Phase 6：必要維運基線 | 部分必要 | 完成關鍵告警、restore與broker rebuild；HA與完整on-call另案處理 |

## Repo 可證實的現況

| 項目 | 現況 | 核心基線仍缺少 |
| --- | --- | --- |
| Release units | FinDB 與 Fetcher 已有獨立 CI/CD、Environment 與 concurrency group | 將 AWS target、role、secret path 與 acceptance 寫入可稽核清冊 |
| CI gate | CD以`workflow_call`執行同一revision的CI；FinDB migration tests已拆為獨立job，並以session-scoped PostgreSQL templates重用historical revisions | 定義支援revision、以實際staging predecessor／restore clone驗證upgrade、image runtime security與deploy bundle deterministic check |
| Image identity | Runtime目前發布GHCR commit SHA tag；FinDB另發布`latest`，Compose有`latest` fallback。Staging改採ECR已決策但尚未建立、apply或驗收 | 建立五個target-specific ECR repositories、保存registry digest、以digest部署並移除所有`latest`部署路徑 |
| EC2 transport | 現行部署仍使用固定完整Action SHA的`appleboy/ssh-action`／`scp-action`；兩份CD已有OIDC＋SSM bounded preflight，AWS端service-specific roles、target tags、managed nodes與command logs已套用。Protected `main` merge SHA `77212ce47b3138c2e21c6984e989b66239fd3cce`的FinDB與Fetcher CD均已通過live OIDC preflight | 日常deployment transport切換留在Phase 4／5；本次成功preflight不等同於SSH／SCP已退場 |
| Runtime secrets | GitHub Environment secrets逐一傳到遠端shell／container | Secrets Manager或SecureString、instance role、輪替與GitHub secret退場 |
| RDS rollout | 有predeploy DB check、writer pause、單一Alembic upgrade與revision check；Console已確認private、encryption、deletion protection、10-day automated backup與PITR inventory | migration credential分權、成功restore rehearsal與release紀錄；RDS tags count為0 |
| Queue | RabbitMQ在FinDB EC2，以root EBS path保存；PostgreSQL是durable truth | current root EBS snapshot／backup policy、容量告警、broker全毀重建演練與實測恢復時間 |
| Fetcher state | 三個provider runtime隔離；container已採non-root、read-only、drop capabilities與no-new-privileges，SQLite與Raw bucket binding只接受current state並fail closed | 一致性備份、SSM rollout、自動化runtime security驗證與完整排程週期觀察 |
| Legacy removal | 舊public routes、舊skill、Fetcher scheduler／SQLite compatibility及DB dataset projection已移除；predeploy仍拒絕非canonical state | 保存staging實際revision及legacy predeploy gates為零的外部證據；不得在新deploy helper恢復compatibility |
| R2 | Raw與Canonical bucket／credential契約已拆分；2026-08-21已人工確認Raw lifecycle 30天與bucket lock 7天 | Canonical runtime不得宣稱已通過資料面驗收 |
| Protection | workflow固定第三方Action SHA；GitHub組織ruleset `Main protection` 要求PR、禁止force-push與限制deletion；repo ruleset `FinDB required CI` 對default branch／`main`強制 `Required CI`；兩個Environment各只允許`main`且無reviewer／wait-timer；兩台EC2已有unit-specific required tags；protected `main`已實證兩個CD不受人工核准阻塞且target count各為1 | 持續維持always-created `Required CI`、Environment branch policy與unit-specific target tags一致；無Phase 1 blocker |
| Observability | 應用內已有health、queue與freshness checks；CloudWatch已有`/findb/staging/findb/ssm`與`/findb/staging/fetcher/ssm`兩個KMS-encrypted、30-day log groups，兩個unit的bounded command stdout已成功寫入；alarms仍為0，RDS Database Insights Standard retention為7天 | EC2／application logs、關鍵alarms、完整retention、告警接收者及synthetic test |
| IaC | Repo已有OpenTofu bootstrap與staging control-plane stacks；encrypted、versioned S3 remote state使用native lockfile與指定KMS key，Tyler (`tylercore`)為state owner；2026-08-26兩個stack plan均為`No changes` | 建立IaC專用GitHub Actions PR plan gate與獨立OIDC plan role；既有EC2／RDS／VPC等data-plane資源只引用與加required tags，不在本計畫全面import；Phase 6 alarms仍待實作 |

`.github/workflows/required-ci.yml`、兩份unit CI、兩份unit CD與
`docker-compose.prod.yml` 是現行行為的 source of truth。本文不把尚未查證的 AWS console
設定當作事實。

## Workflow / release unit matrix

| Unit | CI / CD boundary | Images | Environment | Concurrency | Deploy order |
| --- | --- | --- | --- | --- | --- |
| FinDB | Backend、Dashboard、contracts、Compose、nginx與FinDB workflows | Backend、Dashboard | `staging-findb` | `staging-findb`，`cancel-in-progress: false` | 先部署；contract變更維持backend-first |
| Fetcher | Fetcher、contracts、contract generation依賴與Fetcher workflows | Generic、FinLab、Shioaji | `staging-fetcher` | `staging-fetcher`，`cancel-in-progress: false` | FinDB acceptance後部署 |

所有targeting `main`的PR都建立aggregate `Required CI`；classifier依上述unit邊界呼叫可重用的
`FinDB CI`／`Fetcher CI`，未命中unit時明確接受skipped，classifier失敗、routed job非success或
unrouted job非skipped時fail closed。兩個unit CI不再直接接收`pull_request`，但保留
`workflow_call`與`workflow_dispatch`。

兩個 CD 都由 protected `main` 的 path-filtered push 自動進入 staging，也可手動 dispatch；CD
必須直接呼叫同 revision CI。Staging Environment只允許protected branch且不設人工核准；
production建立後使用`findb-vMAJOR.MINOR.PATCH`或`fetcher-vMAJOR.MINOR.PATCH`宣告對應unit的
release，再由operator手動dispatch artifact promotion；同樣不配置Environment人工核准。

FinDB仍是一個deployment unit，backend與Dashboard各自記錄digest。Fetcher三個image同屬一個
deployment unit，但release manifest必須列出三個digest，不能只用共同SHA tag代替。

## Staging 資源清冊

清冊只記錄target-specific、非敏感的實際識別資料。Secret只記錄ARN或name，不得貼值、hash或
可比較片段。SSH recovery path依Phase 1／6 exit condition管理，不因SSM首次上線立即移除。

| 類別 | `staging-findb` | `staging-fetcher` | 驗收要求 |
| --- | --- | --- | --- |
| AWS account / Region | `439622209937` / `ap-southeast-1`（SSH + IMDSv2、AWS Console 已驗證） | `439622209937` / `ap-southeast-1`（SSH + IMDSv2、AWS Console 已驗證） | Workflow明確檢查STS account與region |
| VPC / subnet | `vpc-0865afcf10442bf4d` / `subnet-0aba2a175b5912c24` | `vpc-0865afcf10442bf4d` / `subnet-0cde9e8dc33bec41e` | RDS private；public EC2若保留須記錄例外與production前檢查點 |
| EC2 target / tags | `i-0942016913367a8b2`（`findb-staging`、`m7i.large`、`ap-southeast-1c`；ARN `arn:aws:ec2:ap-southeast-1:439622209937:instance/i-0942016913367a8b2`）；required tags為`Project=findb`、`Environment=staging`、`DeploymentUnit=findb`、`Owner=tylercore`、`BackupOwner=tylercore` | `i-05f518ef183bc31a9`（`findb-fetcher-staging`、`t3.small`、`ap-southeast-1a`；ARN `arn:aws:ec2:ap-southeast-1:439622209937:instance/i-05f518ef183bc31a9`）；required tags與FinDB相同但`DeploymentUnit=fetcher` | IAM simulator已驗證各deploy role只允許本單元target，跨單元為`implicitDeny`；protected `main` live workflows已分別驗證exact-tag selector只命中1台target |
| EC2 security group / root EBS | `sg-0194615fe18784889`（`ec2-rds-1`）、`sg-070694f093a25cb31`（`launch-wizard-1`）；public inbound HTTP 80、HTTPS 443、SSH 22 from `0.0.0.0/0`，另有 8080 `/32`；`vol-0784675e2ada6642e`（50 GiB）in-use、未加密、delete-on-termination | `sg-0c98f59c6961a00d2`；僅 public inbound SSH 22 from `0.0.0.0/0`，無 public application port；`vol-0201fa5249fe44c39`（30 GiB）in-use、未加密、delete-on-termination | Tyler 為 owner；RabbitMQ／Fetcher state目前均在root EBS。SSH 22依Phase 6 exit gate保留到兩個unit各完成兩次SSM deployment與Session Manager recovery後再移除；補償控制為environment-scoped SSH keys、protected-main audited GitHub deployments、現有recovery reachability與不得擴大SSH使用。EBS補償控制與Phase 6 close condition見下文 |
| Instance role | `findb-staging-instance` profile／role已關聯；只允許FinDB future secret／parameter path、`findb/` deploy bundle、SSM core與FinDB log group | `fetcher-staging-instance` profile／role已關聯；只允許Fetcher future secret／parameter path、`fetcher/` deploy bundle、SSM core與Fetcher log group | Live IMDSv2 preflight已確認兩台profile exact match；不共用runtime path或bundle prefix |
| GitHub deploy role | `arn:aws:iam::439622209937:role/findb-staging-deploy` | `arn:aws:iam::439622209937:role/fetcher-staging-deploy` | OIDC trust的`aud=sts.amazonaws.com`且`sub`精確綁定對應Environment；IAM simulator已驗證cross-unit target/log拒絕，protected `main` workflows已實證各自OIDC assume成功且deploy-role secret read為`AccessDenied` |
| SSM managed node | Online；agent `3.3.4793.0`；Session document `SSM-SessionManagerRunShell-findb-staging` | Online；agent `3.3.4793.0`；Session document `SSM-SessionManagerRunShell-fetcher-staging` | 兩台bounded command與unit-specific Session Manager recovery均成功；command output送各自CloudWatch log group |
| RDS / security group | `fin-db`；ARN `arn:aws:rds:ap-southeast-1:439622209937:db:fin-db`、resource ID `db-ENXUOKJALHEX5BZJ3NVXKN4I7I`；PostgreSQL 16.13、available、`db.t3.micro`、`ap-southeast-1c`、VPC `vpc-0865afcf10442bf4d`、Multi-AZ No、private；`rds-ec2-1` `sg-0faf978bb6c67ca20` 僅允許 5432 from FinDB SG `sg-0194615fe18784889`；connected compute 僅 FinDB；encryption enabled with `aws/rds` KMS、deletion protection enabled、gp3 20 GiB（autoscaling max 1000 GiB）；RDS tags count 0 | RDS不適用；RDS SG沒有Fetcher inbound rule，connected compute也只有FinDB instance | `PubliclyAccessible=false`；只允許FinDB EC2 SG；backup/PITR啟用 |
| Data classification | Staging raw／canonical market data與workflow registry；RDS是durable truth，RabbitMQ可由PostgreSQL重建，generated cache不是source of truth | Provider scheduler／checkpoint SQLite與Raw R2 market payload；不持有RDS、RabbitMQ、Admin或Canonical R2資料 | 資料與credential依unit、target及durability分級，不因同VPC而共用權限 |
| Secret prefix | 規劃`findb/staging/findb/`；目前runtime secrets仍在`staging-findb` Environment，consumer-specific secret ownership與migration留待Phase 2 | 規劃`findb/staging/fetcher/`；目前runtime secrets仍在`staging-fetcher` Environment，consumer-specific secret ownership與migration留待Phase 2 | Tyler 是 resource owner；Phase 2 依 consumer boundary 建立與輪替，不共用DB、R2、provider、Source/Admin或deploy credential |
| Owner | Tyler（GitHub `tylercore`；Phase 1 apply evidence `AdministratorAccess/Tyler`）同時擔任 resource、backup、SSH recovery、alert 與 OpenTofu/IaC remote-state owner | Tyler（GitHub `tylercore`；Phase 1 apply evidence `AdministratorAccess/Tyler`）同時擔任 resource、backup、SSH recovery、alert 與 OpenTofu/IaC remote-state owner | 這是依使用者授權完成的正式 owner assignment，不僅是 provenance；Phase 6 仍須建立並測試 automated notification channel |
| Deploy artifact | Private、versioned、KMS-encrypted bucket `findb-staging-deploy-bundle-439622209937`的`findb/` prefix | 同bucket的`fetcher/` prefix | exact SHA key、checksum、versioning與exact CMK header policy；不使用application R2 bucket |
| CloudWatch | `/findb/staging/findb/ssm`，KMS encryption、retention 30 days；本次command `da88fea4-789f-4e5e-9ead-bea64b9a9480`為Success／response code 0，success marker 1、failure marker 0 | `/findb/staging/fetcher/ssm`，KMS encryption、retention 30 days；本次command `9a5eb744-c6f9-4846-8b85-185cf6a9fe39`為Success／response code 0，success marker 1、failure marker 0 | account／region alarms仍為0；EC2／application logs、required alarms、recipient/channel與synthetic test留待Phase 6 |
| Backup | RDS automated backups enabled 10 days；latest restore time `2026-08-25 13:55 +08`；backup window `08:00-08:30 UTC`；12 available automated snapshots；尚無成功 restore rehearsal。FinDB root EBS 無 current-volume snapshot 或 DLM automated policy | Fetcher root EBS 無 current-volume snapshot 或 DLM automated policy；SQLite/checkpoint state在root EBS | Tyler 接受 current EBS residual risk。RDS remains encrypted durable truth with 10-day backups/PITR；RabbitMQ 可由 PostgreSQL rebuild；generated cache 可由 canonical data regenerate。任何 host-storage replacement／termination 或 storage-destructive maintenance 前，必須先做 current-volume manual snapshot；Fetcher 另須 stopped-writer 或 SQLite online backup，否則 no-go。現有 historical snapshot 不視為 current-volume backup；這些是 policy／conditions，不是既有 snapshots |
| Public endpoint | `/dashboard/`、`/dashboard/lookup` public HTTPS read-only acceptance passed；certificate expiry monitoring 未完成 | 無public inbound endpoint | 外部HTTPS readiness與certificate expiry可監控 |

## Phase 0 evidence（2026-08-25）

本節是本次 Phase 0 完成紀錄。證據來源為 signed-in GitHub settings／Deployments
UI 與 AWS Console 的 read-only 檢視、兩台 EC2 的 SSH + IMDSv2 與 host runtime 檢查、staging
public HTTPS read-only acceptance，以及 repo/local source 檢查；未輸出 secret 值、主機名稱或 IP。
FinDB Alembic inspection使用`uv run`時在現有`ingest` container writable layer同步了25個development
packages；未重啟service或修改database，該暫時layer會由下次normal deployment替換。後續唯讀盤點
不得再用會同步dependency的命令，應使用image內已安裝的entrypoint或immutable probe。

### 本次 Phase 0 work record

| Action | Evidence | Result |
| --- | --- | --- |
| 指派兩個 staging unit 的 owner | GitHub `tylercore` 與 AWS `PowerUserAccess/Tyler` 是本次唯一驗證的 operator identity；兩台 EC2、RDS／資料復原、SSH recovery、alert 與 IaC state ownership 已一併記錄 | Tyler 現為兩個 unit 的 resource、backup、SSH recovery、alert 與 OpenTofu/IaC remote-state owner；這是使用者授權的正式 assignment，不僅是 provenance |
| 設定 Phase 0 interim alert path | GitHub Actions／Deployments 與 AWS Console 可由 `tylercore` 手動查看；CloudWatch inventory 仍為 alarms 0、log groups 0 | Phase 6 前只採 manual monitoring；未宣稱 GitHub notification delivery 或 CloudWatch alarms 已存在，Phase 6 必須建立並測試 automated notification channel |
| 確認 IaC 邊界與 state owner | Repo/local 檢查未找到 Terraform、OpenTofu、CloudFormation、Pulumi、CDK 或 external owner evidence | 新 control-plane resources 選用 OpenTofu，由 Tyler 持有 state；encrypted remote backend／locking 尚未建立，留待 Phase 1/IaC implementation |
| 記錄 SSH／EBS exceptions 與補償控制 | 兩個 EC2 SG 仍允許 public TCP/22；兩個 current root volumes 均未加密、delete-on-termination，且無 current-volume snapshot／DLM policy | Tyler 接受 residual risk。現有控制限於 environment-scoped SSH keys、protected-main audited deployments、recovery reachability、不得擴大 SSH 使用與保留 recovery path；storage-destructive maintenance 必須先做 current-volume manual snapshot，Fetcher 另須 stopped-writer 或 SQLite online backup，否則 no-go |
| 清理 Fetcher local ignored env | `infra/env/staging/fetcher/.env.remote` precondition `CLOUDFLARE_R2_ACCOUNT_ID` count 2、values distinct、mode `0600`；postcondition count 1、remaining nonempty、mode `0600`；equality-only evidence matched GitHub Environment | stale first declaration 已安全移除且未輸出值；其它 intentionally duplicated equal declarations 未變更；ignored file 不應出現在 Git diff |
| 實作並啟用 required CI | 新增always-created `.github/workflows/required-ci.yml`，以fail-closed path classifier呼叫兩份可重用unit CI，最後產生固定名稱 `Required CI`；unit CI移除直接`pull_request` path filters但保留`workflow_call`／`workflow_dispatch`；獨立validator為PASS | 已建立Active repo ruleset `FinDB required CI`（ID `21393782`），只套用default branch／`main`，required context為`Required CI`／Any source，無bypass、不要求branch up-to-date；組織ruleset未變更。Aggregate job名稱與ruleset context是同一契約，後續變更必須同步驗證 |

### GitHub control plane

| 檢查 | 已驗證結果 | 未完成／限制 |
| --- | --- | --- |
| Repository protection | 組織 ruleset `Main protection` 套用 `ai-stock`、`ai-stock-frontend` 與 `findb`：要求 PR、禁止 force-push、限制 deletion；repo ruleset `FinDB required CI`（ID `21393782`）Active且只套用default branch／`main`，強制`Required CI`（Any source），無bypass、不要求branch up-to-date；classic branch protection absent | aggregate workflow已通過YAML、routing、truth-table與獨立驗證；workflow名稱、always-created行為與ruleset context若發生drift會fail closed，任何後續修改都必須在同一變更驗證兩者一致 |
| Deployment Environments | `staging-findb`、`staging-fetcher` 各只允許 selected branch `main`；各有恰一條 deployment branch rule，無 reviewer／wait-timer protection | required target tags／SSM selector 與 environment owner 尚未完成確認 |
| Latest FinDB deployment | FinDB CD #59 成功；active deployment application SHA `394bbd9784367ed190584d3439efa23424d9b1fc` | 這是 SHA tag／deployment evidence，不是 accepted digest manifest |
| Latest Fetcher deployment | Fetcher CD #39；running images application SHA `80333806212c70281e47d9dd837c3642dc4c48f1` | 這是 SHA tag／deployment evidence，不是 accepted digest manifest |

### AWS identity and host evidence

| Unit | EC2／network／storage | Recovery／control-plane 狀態 |
| --- | --- | --- |
| FinDB | `i-0942016913367a8b2`、`findb-staging`、`m7i.large`、AZ `ap-southeast-1c`；ARN `arn:aws:ec2:ap-southeast-1:439622209937:instance/i-0942016913367a8b2`；VPC `vpc-0865afcf10442bf4d`、subnet `subnet-0aba2a175b5912c24`；SG `sg-0194615fe18784889`、`sg-070694f093a25cb31`；root EBS `vol-0784675e2ada6642e`、50 GiB | running、3/3 status checks；IMDSv2 required；僅 `Name` tag；required target tags 尚未建立；`Managed=false`、無 IAM role／instance profile；`amazon-ssm-agent` inactive；SSH recovery reachable with existing environment-scoped SSH keys |
| Fetcher | `i-05f518ef183bc31a9`、`findb-fetcher-staging`、`t3.small`、AZ `ap-southeast-1a`；ARN `arn:aws:ec2:ap-southeast-1:439622209937:instance/i-05f518ef183bc31a9`；VPC `vpc-0865afcf10442bf4d`、subnet `subnet-0cde9e8dc33bec41e`；SG `sg-0c98f59c6961a00d2`；root EBS `vol-0201fa5249fe44c39`、30 GiB | running、3/3 status checks；IMDSv2 required；僅 `Name` tag；required target tags 尚未建立；`Managed=false`、無 IAM role／instance profile；`amazon-ssm-agent` inactive；SSH recovery reachable with existing environment-scoped SSH keys |

Signed-in AWS Console session was `financial_db_dev` in account `439622209937`, with
`PowerUserAccess/Tyler`, region `ap-southeast-1`; together with GitHub `tylercore`, this is the
verified operator identity and Tyler is now the named resource, backup, SSH recovery, alert and
OpenTofu/IaC remote-state owner for both units. Console verified exactly two running EC2 instances in the
account／region, both with 3/3 status checks, but both are public EC2 with unrestricted SSH and no IAM
role／instance profile. Required target tags are absent, so a future SSM tag selector cannot yet be
accepted. RabbitMQ bind path `/var/lib/findb/rabbitmq` is on FinDB root EBS, not a dedicated EBS
volume; all Fetcher SQLite／marker state is on Fetcher root EBS. Current volumes沒有snapshot或DLM
automated policy；these are retained EBS exceptions with Tyler as owner. Existing SSH recovery is reachable, but public SSH from `0.0.0.0/0`
is a recorded Phase 0 exception and is not an acceptable mature baseline; removal remains a later
Phase 6 action after SSM recovery is established.

### FinDB runtime evidence

| 項目 | 已驗證結果 |
| --- | --- |
| Running application | SHA tag `394bbd9784367ed190584d3439efa23424d9b1fc`；`serve`、`ingest`、`dispatcher`、`worker`、`raw-cleanup`、`dashboard`、`nginx`、`rabbitmq` 均 running，已定義 health 者為 healthy |
| Host-local images | Backend `sha256:bd73bdbf5208131041c902a99dd2efefd00aeff33fff5f43e6e6c0e1f27fd6fa`；Dashboard `sha256:52d18fefeeff0efb880804ddee0cc30815e38f3531c9eac84d2bb5df9d853ba3`。兩者皆明確是 host-local image ID，不是 registry digest，也不是 accepted manifest artifact |
| Database / predeploy gates | Alembic `d6e7f8a9b0c1`（head）；`noncanonical_scheduler_control_slots=0`、`noncanonical_dataset_delivery_schedule_slots=0`、`legacy_finlab_scheduler_keys=0`、`dataset_keys_projection_mismatches=0`；dataset projection status `not_applicable`；SSH check passed |

### Fetcher runtime evidence

| 項目 | 已驗證結果 |
| --- | --- |
| Containers / application | 僅三個 stable containers，無 candidate／previous containers；全部以 user `10001:10001` running，root filesystem read-only；SHA tag `80333806212c70281e47d9dd837c3642dc4c48f1` |
| Host-local images | Generic `sha256:109c8963d5792f0848261bbf1726053cbf06bd5069a5a472382cd685aa51467b`；FinLab `sha256:103f570f50c4818b7aa0aabaa384e00c652c6a3f8b27c85668fc7c0df0b3dd4a`；Shioaji `sha256:c4b73a2323e2971bec5d979133b1499012429fdb066ef1d40ef7f30f54ae0b1c`。皆是 host-local image ID，不是 registry digest，也不是 accepted manifest artifact |
| State checks | Generic／FinLab SQLite `user_version=3`，table set 含 `scheduled_job`、`symbol_checkpoint`；Shioaji 為 current provider table set、`user_version=0`、`quick_check=ok`；三者 `quick_check=ok` |
| Filesystem / storage | State dirs UID/GID `10001:10001`、mode `0700`；raw-bucket marker mode `0600`；全部 state 在 root EBS，非 dedicated volume；current-volume snapshot／DLM policy 不存在，例外 owner 為 Tyler |

### Acceptance and repository evidence

- Configured staging host 的 public HTTPS read-only acceptance 已通過 `/dashboard/` 與
  `/dashboard/lookup`；certificate expiry monitoring 仍待完成，未新增或恢復 legacy routes。
- Repo/local 檢查未發現 Terraform、OpenTofu、CloudFormation、Pulumi、CDK 或 remote-state
  ownership evidence；因此新 control plane 選用 OpenTofu，Tyler 擔任 state owner。encrypted remote
  backend／locking 尚未建立，留在 Phase 1/IaC implementation work。
- ignored `infra/env/staging/fetcher/.env.remote` mode `0600` 的 stale first
  `CLOUDFLARE_R2_ACCOUNT_ID` declaration 已安全移除，未輸出 values。Precondition count 為 2 且
  values distinct；postcondition count 為 1、remaining value nonempty、mode 仍為 `0600`。先前
  equality-only evidence 已確認 remaining declaration 與 GitHub Environment value 相符；其它
  intentionally duplicated equal declarations 未變更。該 ignored file 不應出現在 Git diff。
- GitHub actual Environment secret names 與契約相符：FinDB 16 個、Fetcher 13 個；本紀錄不列
  secret values。
- Current operator／deployment actor 為 `tylercore`；Tyler 已被正式指派為兩個 unit 的 resource、
  backup、alert、SSH recovery 與 OpenTofu/IaC remote-state owner。Phase 6 前的 alert channel 是
  `tylercore` 手動監看 GitHub Actions／Deployments 與 AWS Console；GitHub notification delivery
  與 CloudWatch alarms/log groups 均未宣稱已存在，Phase 6 必須建立並測試 automated notification channel。

## 身分、secret 與 release 設計

### GitHub OIDC 與 AWS roles

兩個deploy role的trust policy只接受GitHub OIDC provider，並同時限制：

```text
repository: FPI-TW/findb
aud: sts.amazonaws.com
sub (FinDB):  repo:FPI-TW/findb:environment:staging-findb
sub (Fetcher): repo:FPI-TW/findb:environment:staging-fetcher
```

現行只有deploy job取得`id-token: write`，CI與build job尚未取得AWS身分。ECR foundation建立後，
FinDB與Fetcher build jobs各自改用獨立、main-only的publisher role；trust限定
`repo:FPI-TW/findb:ref:refs/heads/main`與`aud=sts.amazonaws.com`，且只可publish／inspect自身ECR
repositories。Publisher、deploy與instance roles不得共用：deploy role只可查驗target、上傳release
artifact及對對應tag的唯一EC2執行SSM command，不得讀Secrets Manager value、RDS data、R2
credential、另一unit的target或publish image。

EC2 instance role負責讀取自身runtime secrets與deploy bundle。FinDB與Fetcher的deploy role、
instance role、secret path及KMS policy scope不得重用。

### Staging ECR registry

2026-08-27已決策staging改採帳號`439622209937`、區域`ap-southeast-1`內的Amazon ECR；下列
repositories均為待建立／待apply／待驗收，不得從本節推論資源已存在：

- `findb/staging/backend`；
- `findb/staging/dashboard`；
- `findb/staging/fetcher/twelve-data`；
- `findb/staging/fetcher/finlab`；
- `findb/staging/fetcher/shioaji`。

五個repositories皆採private、immutable tags、AES-256與basic scan-on-push；`force_delete=false`，
IaC以`prevent_destroy`保護。Lifecycle只自動清理超過7天的untagged images，不得使用會刪除
accepted digest的blind tagged-image rule；後續如需控制tagged image成本，必須先有manifest-aware
retention並保護所有仍可rollback／promotion的digests。

FinDB與Fetcher各自使用main-only publisher role push自身repositories。兩台EC2只以unit-specific
instance role取得`ecr:GetAuthorizationToken`所需的12小時authorization token，並只pull自身exact
repository ARNs；token只寫入`/run` tmpfs Docker config，命令結束即logout及清理，不寫入Secrets
Manager、GitHub或persistent host設定。Deploy role不得取得ECR publish或runtime pull權限。

Staging完成cutover後不再dual-publish GHCR；既有production GHCR流程暫作相容路徑，直到正式
production ECR repositories與promotion workflow另案完成。本計畫不將該相容路徑誤列為最終
production artifact契約。

### Runtime secrets

預設使用Secrets Manager保存需要版本與輪替紀錄的RDS、provider、R2、DB-backed API key與
RabbitMQ credentials；簡單且低頻變更的敏感值可使用SSM SecureString。ECR authorization token
由instance role即時取得，不是runtime secret。非敏感設定留在GitHub Environment variables或
Parameter Store一般參數。

目前共有19筆Secrets Manager metadata，其中17筆為目標runtime catalog，兩筆
`findb/staging/findb/registry/ghcr-pull`與`findb/staging/fetcher/registry/ghcr-pull`是待退役的空
過渡資源。ECR live acceptance完成後，catalog與IaC active runtime-secret集合必須收斂為17筆；
retirement apply只可排程刪除上述兩筆且保留30天recovery window。兩個已建立但未使用的
package-read-only PAT同樣在ECR驗收後撤銷，文件與操作紀錄均不得保存token內容。

至少維持下列邊界：

- RDS migration credential不得常駐注入application containers；
- RDS application、Canonical publisher與Canonical reader依用途分離；
- Fetcher shared、各provider與Raw R2 credential只供對應consumer；
- Staging EC2不得接收`GITHUB_TOKEN`、PAT或GHCR credential；ECR pull只使用instance role短效token；
- Host-side loader只以allowlist取值，寫入`tmpfs`上的`0600`檔案並在結束後清理；不得echo、寫入
  persistent `.env`、放入SSM command參數或diagnostic output。

### Immutable release 與 promotion

每個CD run只build一次，並產生versioned release manifest：

```json
{
  "schema_version": 1,
  "deployment_target": "staging",
  "deployment_unit": "findb-or-fetcher",
  "commit_sha": "40-char SHA",
  "images": {"name": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/repository@sha256:..."},
  "migration_revision": "alembic revision or none",
  "contract_versions": ["versioned contracts"],
  "deployment_bundle_sha256": "...",
  "created_by_run_id": "..."
}
```

- Build job從ECR取得每個image digest；SHA tag只作索引，不作部署或rollback identity；
- Compose與Fetcher deploy helper只接受完整`image@sha256:...`，缺值或`:latest`一律fail closed；
- Deploy bundle使用private、versioned的AWS S3 control-plane bucket，與Raw／Canonical R2分離；
- Host在停止writer前先驗證manifest、bundle checksum、target、pull exact digests並render config；
- 成功後保存accepted manifest、Alembic revision、SSM command ID與acceptance結果；
- 未來production promotion只能使用staging ECR已接受的相同OCI artifacts與digests，不重新build。

未來production採unit-specific tag加手動promotion：

- FinDB Git tag使用`findb-vMAJOR.MINOR.PATCH`，Fetcher使用`fetcher-vMAJOR.MINOR.PATCH`；tag必須
  指向protected `main`上已有對應accepted staging manifest的commit；
- operator建立Git tag後再manual dispatch對應unit的promotion，workflow必須驗證tag、unit、commit
  與accepted manifest一致；Git tag與manual dispatch共同構成發布授權，不另設Environment reviewer；
- promotion將accepted staging OCI artifact複製到target-specific production ECR repository，
  驗證digest不變後才新增Docker `vMAJOR.MINOR.PATCH` tag；FinDB同時涵蓋backend與Dashboard，
  Fetcher同時涵蓋generic、FinLab與Shioaji；
- Docker release tag已指向相同digest時允許冪等重播，若已指向不同digest則fail closed且不得覆寫；
- Git tag、SHA tag與Docker release tag都只作索引；production manifest從accepted staging
  manifest衍生，實際deploy與rollback仍使用完整digest，不以tag取代image identity。

## IaC 邊界

本repo採OpenTofu管理本計畫新建或為cutover明確修改的控制面資源。Bootstrap stack建立
`findb-staging-tofu-state-439622209937` private、versioned S3 bucket與
`alias/findb-staging-tofu-state` KMS key；bootstrap與control-plane state分別使用
`staging/bootstrap.tfstate`與`staging/control-plane.tfstate`，並以S3 native lockfile鎖定。兩份
state object皆已驗證使用exact state CMK與bucket key，操作期backend設定由ignored
`bootstrap/backend.tf`依tracked template產生，不進入Git history。

本次只納管下列控制面資源：

- GitHub OIDC provider引用、FinDB／Fetcher deploy roles與instance roles；
- 五個staging ECR repositories、FinDB／Fetcher publisher roles、instance pull policies與安全的
  image lifecycle；
- SSM、KMS／secret policies、CloudWatch log groups及必要alarms；
- private、versioned deploy bundle／release manifest S3 bucket與權限。

既有EC2、RDS、VPC、subnet、security groups、R2與DNS先用read-only data source或資源清冊引用。
2026-08-26套用前已排除同名OIDC provider、roles、profiles、documents、KMS aliases、log groups與
buckets碰撞；套用後bootstrap與staging stacks均為`No changes`。全面納管既有AWS data plane
另案進行，不阻塞本次staging hardening；若後續發現外部owner，先import或交由原owner修改，
禁止建立第二套同名資源。

IaC原始碼同步與plan採以下過渡及長期契約：

- IaC專用workflow完成前，CloudShell只接受由受信任operator從指定commit建立、僅含
  `infra/tofu/`的單次archive。上傳前後都驗證SHA-256，從`/tmp`解壓，執行完即刪除source、
  tfvars、plan與archive；source commit變更或合併到`main`後必須以新commit重新產生及驗證，
  不把CloudShell副本視為可持續同步來源；
- 長期由GitHub Actions checkout觸發workflow所屬的exact commit，不再把CloudShell上傳作為日常
  同步方式。`infra/tofu/**`變更的PR必須執行recursive `fmt -check`、backend `init`、`validate`
  與refresh-enabled `plan`，provider及OpenTofu版本依repo lockfile／workflow固定；
- plan job使用獨立`staging-infra-plan` OIDC role。該role只允許讀取既有AWS資源、讀取及解密
  staging state，並只為S3 native lockfile取得必要的建立／刪除鎖權限；不得修改受管AWS資源、
  讀取runtime secret value或取得apply role；
- workflow輸出綁定commit SHA與configuration／lockfile checksum的bounded plan摘要；完整plan不得
  公開或跨commit重用。任何delete／replace action、初始化或驗證錯誤皆fail closed，除非另有具名
  operator、理由、影響範圍與核准紀錄；
- apply不由PR plan role執行。合併protected `main`後，operator以獨立apply身分重新checkout合併
  commit、重新執行相同檢查與fresh plan，確認預期變更且零destroy後才可人工apply。PR plan只作
  gate與預覽，不構成apply授權。

## 實作波次

每一波各自提PR；先完成read-only或dual-path驗證，再移除舊路徑。不得在同一個deployment同時
切換OIDC、secret來源、image identity與migration流程。

### Phase 0：盤點與變更保護

- [x] 填完staging資源清冊，記錄owner、AWS account/region、resource ARN/ID、資料分類、backup
  policy、告警接收者與公開網路例外。
- [x] 驗證`main`的required CI與PR protection；`staging-findb`、`staging-fetcher`只允許protected
  branch且不設Environment人工核准，維持main合併後自動部署。
- [x] 依IaC邊界確認團隊既有工具與state owner；沒有既有標準時採OpenTofu，只納管新控制面；Tyler
  (`tylercore`) 擔任 OpenTofu/IaC remote-state owner，encrypted backend／locking 留待 Phase 1。
- [x] 記錄目前accepted SHA、實際image identity、Alembic revision、running containers、RDS
  snapshot／backup狀態與SSH recovery owner；同時保存noncanonical scheduler、legacy FinLab key與
  dataset projection等predeploy gates為零的結果。

Phase 0 執行註記：四項 checklist 均完成。文件只保存長期有效的控制契約與外部設定證據，
不保存一次性整合狀態。

| Checklist | 已完成的子結果 | 阻塞／未完成 |
| --- | --- | --- |
| 資源清冊 | 已記錄 account、region、VPC、subnet、恰兩台 running EC2 與 3/3 checks、SG、root EBS、RDS private／SG／encryption／backup-PITR state、runtime、公網例外與 public HTTPS 結果；Tyler 已是兩個 unit 的 resource、backup、SSH recovery、alert owner | Required EC2 tags、SSM、CloudWatch alarms/log groups 與 automated notification 仍留在 Phase 1/6；public SSH 與未加密 EBS 是已記錄、由 Tyler 承擔的 residual exceptions |
| `main` 與 Environment protection | 已驗證組織PR／force-push／deletion rules、兩個Environment各僅允許`main`且無reviewer／wait-timer；另建立Active、findb-only `FinDB required CI` ruleset，default branch只要求aggregate `Required CI`。Aggregate與兩份unit workflow變更已通過獨立驗證 | 無Phase 0 blocker；持續條件是每個targeting `main`的PR都產生`Required CI`，且不得以放寬ruleset或恢復path-filtered required contexts繞過 |
| IaC 與 state owner | repo/local 未找到 Terraform、OpenTofu、CloudFormation、Pulumi、CDK 或 remote-state ownership evidence；因此選用 OpenTofu，Tyler (`tylercore`) 擔任 state owner | encrypted remote backend／locking 尚未建立，屬 Phase 1/IaC implementation work；不得宣稱 state backend 已存在 |
| release／復原紀錄 | 已記錄 FinDB／Fetcher SHA、host-local image IDs（非 registry digests）、Alembic head、containers、Fetcher SQLite／filesystem checks、predeploy gates、SSH recovery 可達、RDS automated backup／PITR inventory，以及 `.env.remote` stale declaration 的安全清理結果 | accepted manifest／完整 registry digests、RDS restore rehearsal、current-volume EBS backup與SQLite／RabbitMQ recovery仍屬後續 Phase 3/6 驗收，不是本次已存在的證據 |

Phase 0 exit gate **已達成**：target、database、secret／backup owner與residual exceptions都有具名
紀錄；repo required CI enforcement與既有PR protection已由GitHub設定頁外部驗證；SSH recovery
仍可用，且staging自動部署不會被Environment人工核准阻塞。後續變更必須維持always-created
aggregate workflow、`Required CI` context與ruleset一致，不得倒退為path-filtered required contexts。

### Phase 1：OIDC、SSM與instance role基礎

- [x] 建立GitHub OIDC provider與兩個staging deploy roles，trust精確綁定各自Environment。
- [x] 為兩台EC2建立獨立instance profile、必要target tags、SSM agent與Session Manager設定。
- [x] SSM command output送到unit-specific CloudWatch log group，設定30-day retention且禁止secret輸出。
- [x] 以OIDC執行無副作用preflight：STS account／role、target count恰為1、environment marker、
  Docker／Compose版本、disk／inode、time sync、DNS與instance profile。
- [x] SSM首次live acceptance與兩個unit-specific Session Manager recovery成功；依既定範圍保留
  SSH recovery與TCP/22，不在本Phase提前移除。

#### Phase 1 work record（2026-08-26）

| Action | Evidence | Result |
| --- | --- | --- |
| 建立與遷移OpenTofu state | Bootstrap apply `8 added / 0 changed / 0 destroyed`；state bucket `findb-staging-tofu-state-439622209937`啟用versioning、public access block、native lockfile與KMS；local bootstrap state經驗證後遷移到`staging/bootstrap.tfstate` | Bootstrap與control-plane state objects均有version ID，SSE為`aws:kms`，exact key為`arn:aws:kms:ap-southeast-1:439622209937:key/776159fc-3251-4cd0-98b0-24dfa9e9701d`；兩個stack最終plan均為`No changes` |
| 建立Phase 1 AWS foundation | Staging apply `38 added / 0 changed / 0 destroyed`；建立GitHub OIDC provider、兩個deploy roles、兩個instance roles/profiles、required EC2 tags、兩份Session documents、兩個KMS-encrypted SSM log groups與private deploy-bundle bucket | OIDC issuer `token.actions.githubusercontent.com`、audience `sts.amazonaws.com`；trust subject分別精確綁定`staging-findb`／`staging-fetcher`，deploy bundle以unit prefix與exact CMK policy隔離 |
| 關聯instance profiles並啟用SSM | FinDB association `iip-assoc-025dc561d8df3ea85`；Fetcher association `iip-assoc-09519a3019afd0407`；兩台snap agent均為`3.3.4793.0` | `i-0942016913367a8b2`與`i-05f518ef183bc31a9`均為SSM `Online`，IMDSv2回報的profile與unit exact match |
| Live bounded host acceptance | Run Command驗證instance ID/profile、Docker/Compose、root disk、inode、UTC與`findb-staging.tingfong.com` DNS；兩台response code 0、stderr空白 | FinDB root disk/inode使用18%／4%，Fetcher為24%／7%；stdout已寫入各自CloudWatch log group，不讀取env或runtime config |
| 修正live acceptance發現的最小IAM缺口 | 首次command成功但agent log證明CloudWatch publisher缺`DescribeLogGroups`／unit-scoped`CreateLogGroup`，association loop缺`ListInstanceAssociations`；同時補齊`UpdateInstanceAssociationStatus` | 精確plan為`0 added / 4 changed / 0 destroyed`；修正後兩個stdout streams建立成功，recent agent check無實際AccessDenied／ERROR |
| 驗證recovery與least privilege | 兩份unit-specific Session Manager documents各建立一次session並正常退出；IAM simulator以實際role、target tags與ARN測試 | 本單元`ssm:SendCommand`與log read為`allowed`；cross-unit target/log與deploy-role `secretsmanager:GetSecretValue`為`implicitDeny` |
| 發布GitHub Environment variables | `staging-findb`與`staging-fetcher`各發布`AWS_REGION`、`AWS_ACCOUNT_ID`、unit-specific deploy role ARN、instance profile、SSM log group與DNS check name；透過GitHub API回讀鍵值 | 12個非敏感值與accepted OpenTofu outputs一致；未變更既有Variables、Secrets、branch policy或reviewer設定 |
| 保留既有recovery與secret邊界 | 未修改security groups、SSH keys、TCP/22、runtime secrets、RDS或application containers | SSH與未加密／無backup EBS仍依Phase 0 owner、補償控制與Phase 6條件管理；secret migration只在Phase 2執行 |
| Protected `main` OIDC／SSM preflight | PR [#171](https://github.com/FPI-TW/findb/pull/171)合併為SHA `77212ce47b3138c2e21c6984e989b66239fd3cce`；[FinDB CD 32930274496](https://github.com/FPI-TW/findb/actions/runs/32930274496)與[Fetcher CD 32930274472](https://github.com/FPI-TW/findb/actions/runs/32930274472)均由`push`自動觸發並完成same-revision CI、build與deploy | 兩個Environment均通過STS account／role guard、exact-tag target count 1、exact instance profile、SSM `Online`及deploy-role secret read `AccessDenied`；無reviewer／wait-timer阻塞 |
| 驗證本次bounded command與marker | FinDB command `da88fea4-789f-4e5e-9ead-bea64b9a9480`寫入`/findb/staging/findb/ssm`；Fetcher command `9a5eb744-c6f9-4846-8b85-185cf6a9fe39`寫入`/findb/staging/fetcher/ssm` | 兩個invocation皆為Success、response code 0；以command／instance-specific log stream prefix查得success marker 1、failure marker 0，證實stream建立競態修正後仍維持bounded且fail-closed的marker契約 |
| 部署後read-only acceptance | FinDB與Fetcher runtime皆使用SHA `77212ce47b3138c2e21c6984e989b66239fd3cce`且restart count為0；FinDB Alembic為`d6e7f8a9b0c1`（head），DB-authoritative queue health無DLQ、expired lease、missing delivery或unpublished outbox；公開`/dashboard/`與`/dashboard/lookup`最終HTTPS 200 | Fetcher三個scheduler均以`10001:10001` running；FinDB container未指定non-root user、實際deployment仍使用SSH／SCP、image仍以SHA tag引用，分別保留在Phase 3、Phase 4／5，不誤列為Phase 1完成項目 |

Phase 1 checklist已全部完成。Protected `main`的兩個GitHub Environments已各自取得OIDC token並
跑完workflow bounded preflight；驗收同時以GitHub Actions、AWS SSM invocation與CloudWatch
marker查詢交叉確認，不以AWS administrator session代替GitHub OIDC證據。

Phase 1 exit gate **已達成**：兩個Environment各自只命中一台EC2；cross-unit SSM／log access由
IAM simulator維持`implicitDeny`，live deploy role secret read為`AccessDenied`；兩個unit-specific
Session Manager recovery均可用。

### Phase 2：Runtime secrets與ECR registry cutover

目前狀態以「已決策／待建立／待驗收／驗收後退役」管理，不記錄branch是否已commit或準備合併等
會隨Git history失真的瞬時狀態。FinDB與Fetcher staging hosts均已安裝AWS CLI v2.36.31，各自以
unit-specific instance role完成STS identity驗證，並通過`/run` open-file-descriptor tmpfs檢查。
Secrets Manager已有19筆metadata；17筆目標runtime entries已建立版本，兩筆GHCR pull entries為空
且列為驗收後退役。Staging ECR foundation、publisher roles、workflow cutover與live deployment均仍
待建立或待驗收。既有GitHub Environment secrets、persistent host credentials與SSH recovery必須
保留，直到本Phase其餘驗收全部成功且撤銷條件另行確認。

- [ ] 建立IaC專用GitHub Actions PR plan gate與`staging-infra-plan` OIDC role，依IaC邊界驗證
  exact commit、bounded plan、零delete／replace及plan／apply身分分離；完成前只使用上述
  commit-specific CloudShell archive過渡流程。
- [ ] 依consumer邊界建立target-specific secrets與KMS policy；先寫入新版本，不刪GitHub值。
- [ ] 新增host-side secret loader，以allowlist取值、寫入tmpfs、驗證owner／mode並於結束後清理。
- [ ] 先用現行SSH CD完成一次非資料產生的deploy，改由instance role取secret，確認每個container
  只取得必要credential。
- [ ] Foundation PR建立五個ECR repositories、兩個main-only publisher roles及unit-specific instance
  pull policies；合併後以fresh zero-destroy plan人工apply，驗證immutable tags、AES-256、basic
  scan-on-push與untagged 7天lifecycle。此步不得同時切換workflow。
- [ ] Foundation apply完成後，另以cutover PR將staging build/push/pull切換到ECR；staging與暫留的
  production GHCR相容路徑必須由互斥、fail-closed jobs處理，staging不得dual-publish或把
  `GITHUB_TOKEN`／PAT傳到EC2。FinDB與Fetcher各完成ECR登入、pull與live deployment驗收。
- [ ] 逐一輪替DB-backed keys、R2、provider與RabbitMQ credentials，觀察完整排程週期。
- [ ] 確認last-used與health後，撤銷GitHub Environment及persistent host env中的舊runtime secrets；
  GitHub只保留role ARN、region、target selector、public host與secret identifier等非敏感值。
- [ ] ECR驗收後以retirement PR把active runtime-secret集合收斂為17筆；OpenTofu plan只可刪除兩筆
  空GHCR metadata並使用30天recovery window。同一驗收波次撤銷兩個未使用PAT，不記錄token值。

Exit gate：deploy role無法讀secret value；workflow log與SSM command不含runtime secret；舊
credential已撤銷而非只複製；FinDB與Fetcher無cross-secret read；兩個unit皆以instance role取得
ECR短效token，staging無GHCR credential，active runtime-secret catalog恰為17筆。

### Phase 3：Digest、release manifest與CI gate

- [ ] Build jobs輸出每個image digest並建立manifest；FinDB記錄backend＋Dashboard，Fetcher記錄
  generic＋FinLab＋Shioaji；五個image reference均為完整staging ECR
  `repository@sha256:...`。
- [ ] 建立versioned deploy bundle，納入Compose、nginx templates與deploy helper checksum。
- [ ] Compose與deploy helper改為必填完整image reference；移除FinDB `latest`發布及所有fallback。
- [ ] CI驗證manifest schema、SHA／digest格式、bundle checksum、deterministic generation與config
  render，缺值或`:latest`一律失敗。
- [ ] 保留現有獨立migration CI job、空資料庫upgrade、單一Alembic head與historical regression
  suite；明確列出支援revision，並從Phase 0記錄的實際staging predecessor或其restore clone驗證
  upgrade至candidate head。
- [ ] 對Fetcher現有non-root、read-only root filesystem、drop capabilities、no-new-privileges與
  writable paths加入自動化image/runtime驗證；FinDB backend、Dashboard與Compose須補齊相同基線
  或記錄具體、具期限的例外，並驗證entrypoint與health command。
- [ ] 在不中止服務的情況下，由SSM target pull並inspect所有exact digests。

Exit gate：五個ECR digests可由相同manifest重播且不重新build；tag漂移不影響部署；CI可攔截
不安全的image、manifest與migration。

### Phase 4：FinDB改走SSM

- [ ] 將FinDB deploy job改成OIDC＋SSM；停止workflow傳送runtime secrets並移除SSH／SCP action。
- [ ] Preflight確認RDS TLS、revision、connection headroom、long transaction、backup／PITR與磁碟。
- [ ] 先停`ingest`、`dispatcher`、`worker`、`raw-cleanup`及所有DB writers，再以migration credential
  執行單一Alembic job；`serve`只在schema相容時保留。
- [ ] 啟動candidate後驗證container、internal health、public TLS／readiness、queue topology、
  worker ping、DB queue health與bounded DB transaction；Dashboard public route只驗證
  `/dashboard/`與`/dashboard/lookup`，不得恢復已移除的legacy public routes或skill入口。
- [ ] 保存accepted manifest與SSM command ID；演練application-only previous digest rollback，並
  驗證schema不相容時拒絕回切、writers保持停止。

Exit gate：連續兩次FinDB staging deploy與一次rollback rehearsal不使用SSH；migration失敗不會
啟動新writers；`FINDB_EC2_*` secrets已移除。

### Phase 5：Fetcher改走SSM

- [ ] 將Fetcher deploy與FinLab smoke改成OIDC＋SSM；停止workflow傳送provider、R2與Source
  credentials並移除SSH action。
- [ ] 保留provider-specific state owner／mode、SQLite quick-check、Raw bucket marker與
  single-writer reconciliation全部現行gate；SSM deploy helper只接受current SQLite schema、
  canonical slot identity與provider-scoped Raw marker，不新增legacy migration或compatibility path。
- [ ] 以DB desired state=`stopped`部署三個exact digests；preflight通過後逐一恢復核准的
  scheduler desired state。
- [ ] 驗證cross-provider secret isolation、calendar fail-closed、heartbeat與bounded terminal
  delivery，並觀察至少一個完整排程週期。
- [ ] 演練candidate失敗、首次部署失敗與previous container recovery，不把舊SQLite恢復到新bucket。

Exit gate：連續兩次Fetcher staging deploy不使用SSH；三個provider維持單一writer且無credential
cross-read；`FETCHER_EC2_*` secrets已移除。

### Phase 6：必要監控、復原與SSH退場

- [ ] CloudWatch至少涵蓋EC2 status、disk／inode、Docker restart、RabbitMQ disk／memory、scheduler
  heartbeat與deployment failure；RDS涵蓋free storage、connections、latency與backup failure。
- [ ] 告警接入具名小團隊owner與通知channel，設定log retention並演練一個synthetic alarm；不以
  建立正式24/7 on-call作為完成條件。
- [ ] 驗證RDS encryption、automated backup、PITR與deletion protection，完成一次staging restore
  rehearsal並保存實測恢復時間與資料點。
- [ ] 對Fetcher SQLite執行停止writer或SQLite online backup的一致性備份與復原；對RabbitMQ演練
  由PostgreSQL outbox重建queue，EBS backup只作快速恢復輔助。
- [ ] 驗證generated instrument／macro cache可由canonical data重生；cache volume不列入durable
  backup或restore來源。
- [ ] 兩個 unit 各完成兩次成功的 SSM deployment，且各自完成成功的 Session Manager recovery；
  在此前提成立後，移除兩台 EC2 的 SSH ingress、GitHub SSH secrets 與未使用 key pairs，保留
  SSM break-glass 流程與 audit trail。
- [ ] 為兩個 current root volumes 建立 encrypted replacement／migration 或 encrypted backup
  chain，啟用 automated backup policy，完成 Fetcher SQLite recovery 與 RabbitMQ rebuild rehearsal；
  在上述 evidence 齊備前，不得把現有 historical snapshot 或 policy／condition 描述成 current
  volume backup。

Exit gate：SSM是唯一日常部署與管理路徑；關鍵alarm、RDS restore、SQLite recovery與broker
rebuild都有最近一次成功紀錄；兩個 unit 各有兩次成功 SSM deploy 與 Session Manager recovery；
current EBS 已有 encrypted replacement／backup chain 與 automated policy；SSH deployment identity
已撤銷。

## 每次 cutover 的 go/no-go

只有全部為Yes才執行會停止writer的步驟：

- manifest commit與當次CI revision一致，所有image使用digest；
- target account、region、Environment與唯一EC2 tag match正確；
- previous accepted manifest與schema相容性已判定；
- RDS backup／PITR健康，migration lock/time與connection headroom在門檻內；
- Fetcher scheduler desired state與data caps已記錄，沒有未結束的provider cycle；
- RabbitMQ／Fetcher state volume空間正常，SSM command logs可用；
- 具名operator、觀察窗口、停止條件與forward-fix owner已確認。

No-go時不得用`docker compose down -v`、刪volume、`alembic downgrade`、`stamp head`、mass
delete或R2 object搬移來強迫部署。Schema已改且舊image不相容時保持writers停止，部署包含目前
migration chain的forward fix。

## 完成定義

Staging AWS deployment只有在以下全部有可查證evidence時才算完成：

- [x] aggregate加四個unit GitHub workflows的path、CI、Environment、concurrency與release unit matrix一致。
- [x] Protected `main`自動部署staging且不受Environment人工核准阻塞；required CI與PR protection
  已完成外部驗證。
- [ ] `infra/tofu/**`變更由IaC專用GitHub Actions在exact PR commit執行`fmt/init/validate/plan`；
  `staging-infra-plan` OIDC role不能修改受管資源或讀取runtime secret value，delete／replace
  fail closed，apply只可由protected `main`的fresh plan與獨立身分人工執行。
- [ ] `staging-findb`與`staging-fetcher`有獨立OIDC deploy role、EC2 instance role與secret path。
- [ ] 五個private staging ECR repositories均為immutable、AES-256、basic scan-on-push，且只有
  對應unit的publisher role可push、instance role可pull；兩個unit均以instance role取得短效token，
  staging不保存或傳送GHCR credential。
- [ ] 日常deploy不使用SSH／SCP，EC2不開放SSH ingress，cross-unit IAM測試fail closed。
- [ ] 所有application image以digest部署，accepted release manifest包含五個完整ECR digests、可重播
  並供production promotion。
- [ ] Accepted release manifest具備未來promotion所需的unit、commit與完整digests；契約已固定為
  `findb-vMAJOR.MINOR.PATCH`／`fetcher-vMAJOR.MINOR.PATCH`加manual dispatch，Docker
  `vMAJOR.MINOR.PATCH` tag只能在target-specific production ECR repository附加到相同OCI digest，
  碰撞時fail closed。Production不得重新build或依tag部署；實作production promotion workflow本身
  不是本計畫完成條件。
- [ ] Runtime secrets不在GitHub或persistent host env file，active catalog恰為17筆，兩筆空GHCR
  metadata已排程退役、兩個未使用PAT已撤銷，並完成一次實際runtime credential輪替。
- [ ] RDS private、backup／PITR／deletion protection與一次restore rehearsal有紀錄。
- [ ] Migration停止所有writers，失敗與schema-incompatible rollback路徑已演練。
- [ ] FinDB與Fetcher各完成兩次SSM deploy，通過bounded acceptance與完整排程週期觀察。
- [ ] 關鍵EC2／RDS／EBS／application告警、log retention、owner與synthetic alarm有紀錄。
- [ ] Fetcher SQLite recovery與RabbitMQ由PostgreSQL outbox重建均已演練。
- [ ] 所有staging例外都有owner、補償控制與production前重新評估的觸發條件。
- [ ] 現行操作移入`docs/operations/deployment.md`，migration／recovery規則移入對應operations
  runbook，target設定契約移入`infra/env/`，未完成項目只保留在`docs/dev/backlog.md`。
- [ ] 上述證據已有永久、可查驗的位置；完成此確認的同一PR刪除本暫時性計畫，並更新README、
  文件索引、backlog及其他連結。

## 明確延後與重新評估條件

| 項目 | 本次處理 | 重新評估條件 |
| --- | --- | --- |
| Staging HA／多EC2／ASG／blue-green | 延後；維持單EC2且不宣稱HA | 單機故障開始阻塞release，或production topology需要先演練 |
| RDS Multi-AZ／read replica／proxy | 不作staging完成條件 | Production SLA、連線或讀取負載需求確立 |
| Private subnet＋ALB | 若public EC2移除SSH且只保留必要HTTPS，可暫緩 | Production network設計完成前，或public EC2風險不可接受 |
| 全面AWS IaC import | 延後；本次只納管新控制面 | 既有resource inventory／drift responsibility 已由 Tyler 持有，待 production 環境複製或 drift治理需求確立 |
| Canonical R2資料面驗收 | 延後；只完成bucket與credential邊界 | Canonical publish/read/sign runtime完成 |
| Production資源與promotion workflow | 延後；既有GHCR流程只作暫時相容。本次定義unit-specific Git tag、target-specific production ECR、Docker tag與accepted manifest promotion契約 | 開始建立production Environment、ECR、EC2、RDS、R2及正式promotion workflow |
| 正式24/7 on-call與企業稽核 | 延後；使用具名owner與通知channel | Production SLA、法遵或客戶稽核需求確立 |

這份檔案是暫時性執行計畫，不是永久runbook。不得在尚有未完成Phase、未搬移的操作知識或
只存在本文的驗收證據時提前刪除；全部完成並完成文件搬移後，也不應繼續保留本計畫。
