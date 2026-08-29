# Staging AWS Deployment Completion Plan

> 狀態：Phase 0–1完成；Phase 2A的runtime-secret／ECR cutover、GHCR metadata retirement apply與
> DB-backed runtime credential rotation已完成，僅剩accepted SHA後的完整原生provider cycle時間
> gate，尚未宣告完成；Phase 2B的provider與R2 acceptance-criterion scope項目及RabbitMQ rotation已完成，
> GitHub runtime copies移除仍未完成。本文是staging AWS控制面、部署身分與驗收的核心
> 成熟化計畫；現行可操作 runbook 仍以
> [`../operations/deployment.md`](../operations/deployment.md) 為準。
>
> 最後盤點：2026-08-29。Phase 1 evidence 的 protected `main` merge SHA
> `77212ce47b3138c2e21c6984e989b66239fd3cce` 已由兩個 GitHub Environments 完成 OIDC／SSM
> preflight及既有SSH deployment，OpenTofu remote state、OIDC/IAM、instance profiles、required
> tags、SSM managed nodes、Session Manager與unit-specific SSM logs均有live evidence。Phase 0與
> Phase 1已通過exit gate；「服務可用」仍不等同於Phase 2–6的secret遷移、digest／manifest、
> SSM deployment cutover、監控與災難復原已完成。Staging 已完成 Amazon ECR foundation、publisher
> roles、workflow cutover與live deployment驗收；Phase 3 foundation的五枚digest manifest已完成兩個unit的
> artifact、host RepoDigest與合併後自動化SSM exact-digest preflight驗收。Phase 2A的完整provider cycle，以及Phase 2B的GitHub
> runtime copies移除仍未完成。Phase 2B的provider與R2 acceptance-criterion scope項目已依使用者
> 核准完成，RabbitMQ rotation已有live evidence。GHCR metadata retirement apply已完成，兩筆實體metadata待2026-09-27
> recovery window
> 結束後刪除。當前自動化preflight acceptance為兩個unit共同的protected `main` merge SHA
> `228989afe857c82d619cd53d15dbb29873d6710a`。

## ECR foundation 與啟用契約（已完成；持續驗收）

Staging ECR foundation已建立並完成live驗收：五個private repositories、main-only publisher roles、
instance pull權限與read-only infra plan refresh權限均有AWS及workflow evidence。後續任何ECR resource、
IAM role、GitHub variable、OpenTofu apply或deployment變更仍須各自授權與驗收。

`STAGING_ECR_CUTOVER_ENABLED` 是唯一的 repository variable activation gate。啟用順序如下：

1. gate 維持未設定或 `false`；在受保護 `main` 上，另行授權執行 fresh zero-delete plan 與 foundation
   apply，建立 ECR/IAM resources。
2. 先完成 foundation-level acceptance：確認五個 repositories 與 exact settings、publisher/deploy/
   instance role boundaries，以及安全的 authentication／authorization checks（包括 instance role
   只能取得 authorization token 並 pull 自身允許的 images）。此階段不要求 gated application rollout。
3. foundation acceptance 完成後，operator 才可把 repository variable 設為**精確字串** `true`，並手動
   trigger staging workflows。
4. triggered workflows publish immutable commit-SHA images並執行host rollout；其後完成cutover的
   live acceptance與observation。若acceptance失敗，立即將gate設回`false`，這只會freeze並阻擋
   新的staging rollout，**不是**GHCR fallback。此流程已以目前accepted SHA
   `570e3c1e935210c7f084c9d8b5a7f711cdf9300b`完成live deployment驗收。

任何其他值一律 fail closed，staging 不可 build/push/deploy 到 ECR。

啟用後 staging 僅使用 `439622209937.dkr.ecr.ap-southeast-1.amazonaws.com` 和 AWS Secrets Manager
runtime mode；EC2 不接收 `GITHUB_TOKEN`、PAT、`GHCR_USERNAME` 或 `GHCR_TOKEN`。Production 維持
GHCR 與既有 GitHub runtime secret 相容路徑，且不會 assume staging publisher role 或拉取 staging ECR。
這一輪暫以 immutable commit SHA tag 部署；digest manifest／promotion 是明確保留給 Phase 3 的邊界。

受控 rollback 不會恢復 transitional GHCR access。gate 維持精確 `true`，operator 在 protected `main`
手動 dispatch 對應 FinDB 或 Fetcher workflow，選擇 `deployment_target=staging` 並填入已接受的
小寫 40-hex `image_tag`。有指定 `image_tag` 時 workflow 先驗證該 commit 可從當前 protected `main`
到達，並確認 current checkout 的精確 staging runtime inputs 與舊 image SHA 無差異：FinDB 的
`docker-compose.prod.yml`、三個 staging-rendered nginx inputs、三個 renderer 與其 host loader／command／
render／install／deploy／catalog；Fetcher 的 host loader／command／provider release／catalog。只有契約不變
時才允許 current scripts 搭配 old images，否則 fail closed，必須先做 forward compatibility fix。FinDB 只驗證
其 backend、dashboard 兩個固定 staging ECR repositories；Fetcher 只驗證其 Twelve Data、FinLab、Shioaji
三個固定 staging ECR repositories。任一所屬 repository 不存在該 immutable SHA tag 即在 host rollout 前
失敗，絕不重建目前 SHA。此介面不接受 mutable tag、非 main artifact、跨 repository 或 registry image；
rollback 後仍須完整記錄 SHA 並重跑單元 acceptance。這不是 digest promotion，Phase 3 範圍不因此提前實作。

## 決策背景

本計畫依下列已確認前提收斂，不以完整企業級治理作為 staging 完成條件：

- staging 是 production 的前置驗證站，預期三個月內開始建立 production；
- staging 由小團隊共同維運，不要求正式 24/7 on-call；
- 本計畫 pre-cutover 原先曾規劃 protected `main` 合併後自動部署 staging；目前實際行為為
  FinDB／Fetcher 均僅接受 protected `main` 上的 manual dispatch，production採unit-specific
  Git tag加手動 artifact promotion，不另設Environment人工核准；
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
| Phase 2A：Runtime-secret transport與ECR registry cutover | 必要 | 避免runtime secrets經GitHub runner與遠端shell傳遞；改由instance role讀取Secrets Manager、取得ECR短效token，並完成DB-backed credential rotation與GHCR credential退役 |
| Phase 2B：External credential hygiene | 必要但獨立收尾 | Raw／Canonical R2與三個provider scope項目均依使用者核准變更acceptance criterion而完成，既有值維持；R2未建立新key、未輪替、未替換、未撤銷舊key，故不構成rotation或old-value invalidation evidence，也不代表曾執行Cloudflare操作。RabbitMQ已完成rotation與舊值拒絕驗證。GitHub Environment runtime copies保留為獨立未完成項：完整原生provider cycle gate通過後，仍須另行授權、確認last-used與health才可移除；不回頭阻塞ECR cutover判定 |
| Phase 3：Digest、manifest、CI gate | 必要 | staging 必須能重播並把相同 artifact promotion 到 production，不能依賴 `latest` 或可漂移 tag |
| Phase 4：FinDB SSM cutover | 必要 | 保留既有 migration 與 queue safety gate，只替換部署傳輸與secret來源 |
| Phase 5：Fetcher SSM cutover | 必要 | 保留 SQLite、Raw bucket binding 與 single-writer gate，只替換部署傳輸與secret來源 |
| Phase 6：必要維運基線 | 部分必要 | 完成關鍵告警、restore與broker rebuild；HA與完整on-call另案處理 |

## Repo 可證實的現況

| 項目 | 現況 | 核心基線仍缺少 |
| --- | --- | --- |
| Release units | FinDB 與 Fetcher 已有獨立 CI/CD、Environment 與 concurrency group | 將 AWS target、role、secret path 與 acceptance 寫入可稽核清冊 |
| CI gate | CD以`workflow_call`執行同一revision的CI；FinDB migration tests已拆為獨立job，並以session-scoped PostgreSQL templates重用historical revisions | 定義支援revision、以實際staging predecessor／restore clone驗證upgrade、image runtime security與deploy bundle deterministic check |
| Image identity | Staging已由五個target-specific ECR repositories發布及部署immutable commit SHA tags，且不再dual-publish GHCR；production仍保留獨立GHCR相容路徑 | 保存registry digest、以digest部署並移除staging所有tag-based deployment依賴 |
| EC2 transport | 現行部署仍使用固定完整Action SHA的`appleboy/ssh-action`／`scp-action`；兩份CD已有OIDC＋SSM bounded preflight，AWS端service-specific roles、target tags、managed nodes與command logs已套用。Protected `main` merge SHA `77212ce47b3138c2e21c6984e989b66239fd3cce`的FinDB與Fetcher CD均已通過live OIDC preflight | 日常deployment transport切換留在Phase 4／5；本次成功preflight不等同於SSH／SCP已退場 |
| Runtime secrets | Staging已由instance role讀取Secrets Manager，host loader只在`/run` tmpfs建立allowlisted bundle並於使用後清理；GitHub Environment的舊runtime copies仍保留但不是staging runtime source | 完整原生provider cycle gate通過後，另行取得移除授權、確認last-used與health再移除GitHub runtime copies；SSH recovery secrets依Phase 6退場 |
| RDS rollout | 有predeploy DB check、writer pause、單一Alembic upgrade與revision check；Console已確認private、encryption、deletion protection、10-day automated backup與PITR inventory | migration credential分權、成功restore rehearsal與release紀錄；RDS tags count為0 |
| Queue | RabbitMQ在FinDB EC2，以root EBS path保存；PostgreSQL是durable truth | current root EBS snapshot／backup policy、容量告警、broker全毀重建演練與實測恢復時間 |
| Fetcher state | 三個provider runtime隔離；container已採non-root、read-only、drop capabilities與no-new-privileges，SQLite與Raw bucket binding只接受current state並fail closed | 一致性備份、SSM rollout、自動化runtime security驗證與完整排程週期觀察 |
| Legacy removal | 舊public routes、舊skill、Fetcher scheduler／SQLite compatibility及DB dataset projection已移除；predeploy仍拒絕非canonical state | 保存staging實際revision及legacy predeploy gates為零的外部證據；不得在新deploy helper恢復compatibility |
| R2 | Raw與Canonical bucket／credential契約已拆分；2026-08-21已人工確認Raw lifecycle 30天與bucket lock 7天 | Canonical runtime不得宣稱已通過資料面驗收 |
| Protection | workflow固定第三方Action SHA；GitHub組織ruleset `Main protection` 要求PR、禁止force-push與限制deletion；repo ruleset `FinDB required CI` 對default branch／`main`強制 `Required CI`；兩個Environment各只允許`main`且無reviewer／wait-timer；兩台EC2已有unit-specific required tags；protected `main`已實證兩個CD不受人工核准阻塞且target count各為1 | 持續維持always-created `Required CI`、Environment branch policy與unit-specific target tags一致；無Phase 1 blocker |
| Observability | 應用內已有health、queue與freshness checks；CloudWatch已有`/findb/staging/findb/ssm`與`/findb/staging/fetcher/ssm`兩個KMS-encrypted、30-day log groups，兩個unit的bounded command stdout已成功寫入；alarms仍為0，RDS Database Insights Standard retention為7天 | EC2／application logs、關鍵alarms、完整retention、告警接收者及synthetic test |
| IaC | Repo已有OpenTofu bootstrap與staging control-plane stacks；encrypted、versioned S3 remote state使用native lockfile與指定KMS key，Tyler (`tylercore`)為state owner；IaC專用GitHub Actions PR plan gate與獨立OIDC plan role已完成live驗收 | 既有EC2／RDS／VPC等data-plane資源只引用與加required tags，不在本計畫全面import；Phase 6 alarms仍待實作 |

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

兩個 CD 的 protected `main` path-filtered push 只執行同 revision CI 與完成 no-op bridge；staging
ECR build、publish、host rollout 一律由 manual dispatch 啟動。CD 必須直接呼叫同 revision CI。
Staging Environment只允許protected branch且不設人工核准；
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

CI jobs不取得AWS身分；deploy與ECR build/push jobs只在各自職責需要時取得`id-token: write`。
FinDB與Fetcher build/push jobs各自使用獨立、main-only的publisher role；trust限定
`repo:FPI-TW/findb:ref:refs/heads/main`與`aud=sts.amazonaws.com`，且只可publish／inspect自身ECR
repositories。Publisher、deploy與instance roles不得共用：deploy role只可查驗target、上傳release
artifact及對對應tag的唯一EC2執行SSM command，不得讀Secrets Manager value、RDS data、R2
credential、另一unit的target或publish image。

EC2 instance role負責讀取自身runtime secrets與deploy bundle。FinDB與Fetcher的deploy role、
instance role、secret path及KMS policy scope不得重用。

### Staging ECR registry

Staging已在帳號`439622209937`、區域`ap-southeast-1`建立並驗收下列Amazon ECR repositories：

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

目前active runtime catalog與IaC集合均為17筆。兩筆空過渡資源
`findb/staging/findb/registry/ghcr-pull`與`findb/staging/fetcher/registry/ghcr-pull`已在
2026-08-28以retirement apply排程30天刪除；包含planned-deletion metadata時清冊仍顯示19筆。
兩個已建立但未使用的package-read-only PAT已撤銷，文件與操作紀錄均不得保存token內容。

Retirement PR 的 CI 僅是 preflight，不能作為 apply authority。實際刪除只能在 reviewed retirement
PR 合併到 protected `main` 後進行：operator 必須以乾淨 checkout 讓 `HEAD` 精確等於合併後的
`origin/main` SHA，live 驗證兩個 exact Secret ID 都沒有 versions 或 values，從該 SHA 產生 fresh
saved plan，並以 immutable guard 加上兩個 exact allow addresses 驗證 `delete_count=2` 且沒有其他
delete。取得 action-time user confirmation 後，獨立 apply identity 才可 apply 那份 exact saved
plan；隨後驗證兩筆皆為30天 scheduled deletion、active catalog 為17，並重跑 fresh zero-delete plan。

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
  "unit": "findb-or-fetcher",
  "commit_sha": "40-char SHA",
  "images": {"name": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/repository@sha256:..."},
  "migration_revision": "alembic revision or none",
  "contract_versions": ["versioned contracts"],
  "contract_manifest_sha256": "selected contract manifest canonical semantic SHA-256",
  "deployment_source_bundle_sha256": "...",
  "created_by_run_id": "..."
}
```

Phase 3 foundation 的 selected tool 會先執行 `protocol`，且 raw stdout 必須精確為
`findb-release-manifest-v1\n`（stderr、extra bytes 或失敗皆 fail closed）。只有 selected Git tree 中
完全沒有該 tool 的 protected-main explicit SHA rollback 才是 pre-foundation legacy；存在但 protocol 不相容
不能降級為 legacy。

同一份 selected source root 由 descriptor-safe root FD pin 住；每個 allowlisted path 的首次 bytes 讀取會
固定於單一 snapshot，contract parser、schema checksum 與 source checksum 都重用該 bytes，避免重開
pathname 導致的 source drift。

validate 還會精確綁定 workflow 的 unit、selected commit、migration revision、run ID 與每個 expected image
digest ref。manifest output 只寫入預先存在的 job-private runner temp parent；工具從 filesystem root 以
`O_NOFOLLOW` 逐段走訪並 pin parent directory FD，降低 ancestor/path/symlink swap，但不宣稱能防禦同 UID
的任意持續寫入者。

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
  公開或跨commit重用。預設任何delete／replace action、初始化或驗證錯誤皆fail closed；retirement
  PR 的唯一例外只允許兩個 exact metadata deletes，且仍僅為 preflight；
- apply不由PR plan role執行。retirement apply 必須在 reviewed PR 合併後，以 `HEAD` 精確等於
  `origin/main` merged SHA 的乾淨 checkout、live empty-secret 驗證、fresh saved plan、immutable
  guard 的 exact two-delete proof 與 action-time user confirmation 為前提，再由獨立 apply identity
  apply exact saved plan；之後驗證30天 scheduled deletion、17筆 active catalog及fresh zero-delete
  plan。PR plan只作 gate與預覽，不構成apply授權。

## 實作波次

每一波各自提PR；先完成read-only或dual-path驗證，再移除舊路徑。不得在同一個deployment同時
切換OIDC、secret來源、image identity與migration流程。

### Phase 0：盤點與變更保護

- [x] 填完staging資源清冊，記錄owner、AWS account/region、resource ARN/ID、資料分類、backup
  policy、告警接收者與公開網路例外。
- [x] 驗證`main`的required CI與PR protection；`staging-findb`、`staging-fetcher`只允許protected
  branch且不設Environment人工核准；目前 staging 部署由 protected `main` 上的 manual dispatch
  啟動。
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
仍可用，且staging manual dispatch 不會被Environment人工核准阻塞。後續變更必須維持always-created
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

FinDB與Fetcher staging hosts均已安裝AWS CLI v2.36.31，各自以unit-specific instance role完成STS
identity驗證，並通過`/run` open-file-descriptor tmpfs檢查。Staging ECR foundation、publisher roles、
workflow cutover與live deployment 已完成。Secrets Manager active catalog 已收斂為17筆；兩筆空
GHCR transitional metadata已在2026-08-28排程30天刪除，且均無secret version。兩枚staging GHCR
pull PAT已刪除；已知legacy `/opt/findb/.env`與`/opt/findb-fetcher/.env`均不存在；FinDB的
`/run/findb-runtime-secrets`只保留nginx運行所需的`serve-key.conf`，兩台host皆無暫存bundle殘留。
GitHub Environment的runtime copies必須保留到完整原生provider cycle gate通過，並另行取得移除授權及完成
last-used與health確認。七枚DB-backed runtime
credentials已完成輪替、部署、使用驗證與舊值撤銷；三個provider scope項目依2026-08-29使用者核准變更
acceptance criterion而視為完成，既有值維持，實際未輪替、未替換、未撤銷，且不構成rotation evidence；
RabbitMQ已完成rotation及舊password拒絕驗證。Raw與Canonical R2 scope亦依本次使用者核准的
acceptance criterion變更而完成；既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，因此不構成
rotation或old-value invalidation evidence，也不代表曾執行Cloudflare操作。

為避免ECR cutover被不同權限、停機窗口與外部服務流程無限延長，Phase 2自2026-08-29起依
責任邊界收斂為兩段：**2A ECR／runtime-secret transport**只剩accepted SHA後的原生排程週期
觀察；**2B credential hygiene**承接R2與provider scope decision、RabbitMQ rotation及GitHub
Environment runtime copies移除。provider與R2 scope項目已依使用者核准變更acceptance criterion而完成，並非
rotation evidence；RabbitMQ驗收已完成；GitHub copies仍是未完成的獨立工作，完整原生provider cycle gate通過後
仍須另行取得移除授權及確認last-used與health才可撤銷；但它不阻塞ECR cutover本身的完成判定。

- [x] 建立IaC專用GitHub Actions PR plan gate與`staging-infra-plan` OIDC role，驗證 exact commit、
  bounded plan與plan／apply身分分離；PR plan只作preflight。Retirement另以protected-`main`
  fresh saved plan、immutable guard、獨立apply身分與action-time confirmation完成。
- [x] 依consumer邊界建立target-specific secrets與KMS policy；17筆 active runtime entries已建立，
  GitHub值尚未刪除。
- [x] 新增host-side secret loader，以allowlist取值、寫入tmpfs、驗證owner／mode並於結束後清理。
- [x] 完成runtime-secret deployment驗收；每個container僅取得必要credential。
- [x] Foundation 已建立五個ECR repositories、兩個main-only publisher roles及unit-specific instance
  pull policies，並完成 immutable tags、AES-256、basic scan-on-push 與 untagged 7天 lifecycle 驗收。
- [x] staging build/push/pull 已切換到ECR，FinDB與Fetcher完成ECR登入、pull與live deployment驗收；
  staging不dual-publish且不把`GITHUB_TOKEN`／PAT傳到EC2。
- [x] 七枚DB-backed keys已逐一輪替、驗證last-used並撤銷舊值。
- [ ] 在accepted SHA後觀察一次完整原生排程週期；不得以repair rerun取代。
- [x] Phase 2B provider scope change：Twelve Data、FinLab、Shioaji provider scope項目依2026-08-29使用者
  核准變更acceptance criterion而視為完成；既有值維持，實際未輪替、未替換、未撤銷，且不構成rotation evidence。
- [x] Phase 2B RabbitMQ rotation：依安全停機順序重建broker相關服務，確認新值生效、舊password被拒絕、
  舊cookie失效、queue／DLQ與DB-authoritative health正常；詳見[Durable Ingestion Runbook](../operations/ingestion.md#rabbitmq-runtime-credential-rotation)。
- [x] Phase 2B R2 scope change：Raw與Canonical R2依2026-08-29使用者核准變更acceptance criterion而視為完成；
  既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，故不構成rotation或old-value invalidation evidence，
  也不代表曾執行Cloudflare操作。
- [ ] Phase 2B在完整原生provider cycle gate通過後，另行取得移除授權並確認last-used與health，再撤銷GitHub
  Environment中的runtime copies；此項不與R2實際rotation綁定。
  GitHub只保留role ARN、region、target selector、public host與secret identifier等非敏感值。
- [x] 兩枚 staging GHCR pull PAT 已刪除；不記錄token值。
- [x] 以retirement PR把active runtime-secret集合收斂為17筆；apply只刪除兩筆空GHCR metadata，
  使用30天recovery window，並符合本節的protected-`main`、live verification、saved plan、
  immutable guard與action-time confirmation邊界。

#### Phase 2 work record（2026-08-28–29）

| Action | Evidence | Result |
| --- | --- | --- |
| ECR foundation 與 cutover | Staging ECR foundation、publisher roles、workflow cutover及FinDB／Fetcher live deployments已完成驗收 | staging 僅由instance role取得ECR短效token，不保留GHCR credential |
| Runtime secret retirement | Retirement apply由protected `main` SHA `e995fa251f86627982d8be292905bc933ac776f6`產生fresh saved plan，guard精確證明`0 add / 1 change / 2 destroy`且僅含兩個核准地址 | CloudTrail兩筆`DeleteSecret`均於2026-08-28 08:09:03Z成功、`recoveryWindowInDays=30`、無`forceDeleteWithoutRecovery`；預定2026-09-27刪除 |
| Runtime secret inventory | `list-secrets --include-planned-deletion`回報17筆無`DeletedDate`的active entries加兩筆planned-deletion GHCR metadata；兩筆GHCR各為0個version | 17筆active secret各有且僅有一個`AWSCURRENT`；application DB及七筆已輪替DB-backed secret保留`AWSPREVIOUS`供版本稽核；provider與Raw／Canonical R2 scope項目依使用者核准變更acceptance criterion而完成。R2既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，非rotation或old-value invalidation evidence，亦非Cloudflare操作證據；RabbitMQ已完成本輪rotation |
| Legacy GHCR／host material | 兩枚staging GHCR pull PAT已刪除；兩台host均無legacy persistent `.env`；FinDB tmpfs只保留nginx運行所需檔案，其餘暫存bundle已清理 | GitHub Environment目前仍有`staging-findb` 16筆、`staging-fetcher` 13筆secret entries（含runtime copies與SSH recovery）；完整原生provider cycle gate通過後，仍須另行取得移除授權並確認last-used與health，才可撤銷runtime copies |
| 合併版本部署與EOD repair | Manual FinDB run [33161513278](https://github.com/FPI-TW/findb/actions/runs/33161513278)成功部署merge SHA `0e2e28089498237b4261169aa0c6885f215a1d9e`；六個FinDB service使用該ECR SHA，queue／DLQ active gauges為0 | 原FinLab run因application role嘗試partition DDL而失敗；修正後精確rerun `01a047d7-d0f4-7477-9d9f-ceb0eb36959a`一次完成、attempt 1、2/2 rows、failure為null，application role仍無`public CREATE` |
| Scheduler observation | Shioaji兩個feed在2026-08-28 fresh；Twelve Data在2026-08-27 fresh；repair rerun成功但freshness契約明確排除`is_rerun` | 完整三provider post-deploy原生排程週期仍待2026-08-29 Twelve Data 00:15Z及FinLab／Shioaji 06:30Z後驗收，rerun不得取代此gate |
| Fetcher DB-backed credential rotation | Manual Fetcher run [33163966538](https://github.com/FPI-TW/findb/actions/runs/33163966538)成功部署accepted SHA；calendar Serve與Twelve Data、FinLab、Shioaji Source consumer fingerprint均精確對應四枚新credential，且有持續last-used／usage evidence | 四枚被取代credential及FinLab自2026-07-30後未使用的更舊前身共五枚均已撤銷；三個scheduler維持ECR accepted SHA運行 |
| FinDB DB-backed credential rotation | Lookup Serve、static-cache Serve與queue-health Admin三枚credential已輪替至各自Secrets Manager secret；Manual FinDB run [33164657004](https://github.com/FPI-TW/findb/actions/runs/33164657004)通過CI、runtime-secret canary、部署、queue health、public routes與cache生成 | serve／ingest／nginx實際載入fingerprint均精確對應新credential；三枚新key各有200 response與durable usage evidence後，三枚被取代credential均已撤銷 |
| Provider acceptance scope change | 2026-08-29使用者明確核准變更Twelve Data、FinLab、Shioaji的acceptance criterion | 此provider scope項目視為完成；既有值維持，實際未輪替、未替換、未撤銷，且不構成rotation evidence，也不以此宣告原生provider cycle gate通過 |
| R2 acceptance scope change | 2026-08-29使用者明確核准Raw與Canonical R2維持目前值，並變更本PR的acceptance criterion | 此R2 scope項目視為完成；既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，故不構成rotation或old-value invalidation evidence，也不代表曾執行Cloudflare操作 |
| RabbitMQ runtime credential rotation | 先停止三個provider stable schedulers；持久化volume下`RABBITMQ_DEFAULT_PASS`不會更新既有internal user，故先以新`AWSCURRENT` password對`rabbit@findb-rabbitmq`執行in-broker password更新並驗證新auth成功，再於05:04Z由protected `main` deploy重建rabbit、policy、dispatcher與worker。Manual FinDB run [33235103928](https://github.com/FPI-TW/findb/actions/runs/33235103928)在workflow SHA `945abef69aa149da3460f5ce772c3022264bd904`重用accepted image `570e3c1e935210c7f084c9d8b5a7f711cdf9300b`，全綠 | `findb/staging/findb/rabbitmq/runtime`舊version `65fbff30-c417-54b5-b582-0b0fc0f89bc4`仍為`AWSPREVIOUS`，新version `1b2a8db6-6c97-44c0-a21f-9cf384c2571d`為`AWSCURRENT`。official container以新cookie重建後，在相同image、network、node `rabbit@findb-rabbitmq`與相同probe command下，current兩次為`[0,0]`，previous兩次為`[69,69]`，並診斷為cookie authentication rejection；`69`只屬本次歷史結果。舊password被broker拒絕；rabbit healthy、policy exit 0、worker healthy，queue／DLQ、expired leases、missing deliveries與unpublished outbox皆為0，worker heartbeat正常；persistent volume、node、vhost與topology仍存在 |
| Provider scheduler recovery after RabbitMQ rotation | 三個provider stable containers於05:06:47Z恢復running、restart count 0，仍使用accepted ECR SHA `0e2e28089498237b4261169aa0c6885f215a1d9e` | 僅記錄scheduler恢復；沒有原生provider cycle完成的實證，Phase 2A時間gate仍保持未完成 |
| Nginx lookup key重新掛載修復 | 舊lookup credential撤銷後，同源Referer的public Serve request回403；host tmpfs檔已是新fingerprint，但nginx對外仍注入已撤銷的舊key。PR #188與#189雖全綠，live ID／events證明nginx未replace；根因是`bash -s`內的main Compose up繼承stdin並消耗其後script。PR #190將不需stdin的Docker／Compose commands全部隔離為`</dev/null`，合併SHA `570e3c1e935210c7f084c9d8b5a7f711cdf9300b`後manual run [33193026381](https://github.com/FPI-TW/findb/actions/runs/33193026381)完成 | Deploy log在新nginx create／start後輸出`findb_aws_deploy=ready`；live events精確證明舊ID `8f16b2e98ffa`已kill／stop／die／destroy、新ID `c1c2ac638a05`已create／start且healthy。新container具正確Compose labels及唯讀`/run/findb-runtime-secrets/nginx/serve-key.conf` mount，internal exact-Referer與public lookup皆200，六個application containers均為accepted SHA；獨立Validator判定PASS |

Phase 2A exit gate：deploy role無法讀secret value；workflow log與SSM command不含runtime
secret；七枚DB-backed舊credential已撤銷而非只複製；FinDB與Fetcher無cross-secret read；兩個
unit皆以instance role取得ECR短效token，staging無GHCR credential，active runtime-secret catalog
恰為17筆；accepted SHA後的完整原生排程週期通過。除最後一項時間門檻外均已通過。

Phase 2B exit gate：provider與Raw／Canonical R2 scope項目均依使用者核准變更acceptance criterion而視為完成。
R2既有值維持，未建立新key、未輪替、未替換、未撤銷舊key，故不構成rotation或old-value invalidation evidence，
也不代表曾執行Cloudflare操作。RabbitMQ已完成新舊值overlap、consumer reload、health及舊值失效驗證
（`AWSPREVIOUS`保留供稽核，非已刪除）。GitHub Environment runtime copies維持不動，直到完整原生provider
cycle gate通過後另行取得移除授權，並完成last-used與health確認；此待辦不與R2實際rotation綁定。

### Phase 3：Digest、release manifest與CI gate

Phase 3 foundation 已開始並完成 repository／CI artifact 能力：staging build helper 在 immutable
SHA tag reuse 或 push 後均由 ECR 讀取並驗證單一完整 digest；兩個 staging build job 生成、驗證並
上傳 deterministic、unit-scoped manifest。FinDB artifact 記錄 backend＋Dashboard，Fetcher artifact
記錄 Twelve Data＋FinLab＋Shioaji，所有 image reference 都是完整 staging ECR
`repository@sha256:...`。bundle checksum 只涵蓋明確 allowlisted 的 repo-relative staging Compose、
三個 staging nginx inputs、三個 renderer 與 deploy/runtime-secret helper/catalog，另含 selected contracts
manifest 與 manifest tool 自身；不納入 current CI-only ECR build helper、secret 或 env。這是 deterministic
source/template checksum contract，不是 render 後的 host deploy bundle、可重播／versioned packaged bundle，
也尚未有 private S3 persistence。
image `contract_versions` 則由 selected `ECR_IMAGE_TAG` source root 的 `contracts/manifest.json` 取得；工具以
同一 pinned root FD descriptor-safe 讀取每個 entry 指向的 schema，並要求實際 schema SHA-256 等於 entry
宣告值，才把 canonical semantic manifest SHA-256 寫入 artifact。workflow 不另行 materialize 第二份 contract
manifest。bundle checksum仍是 selected commit 的 deterministic source/template inputs（包含 selected contract
manifest content digest），不是 render 後 host deploy bundle、accepted replay bundle 或 live deploy evidence。

artifact 名稱固定映射為 `findb` →
`staging-findb-release-manifest-${{ github.run_id }}-${{ github.run_attempt }}`、`fetcher` →
`staging-fetcher-release-manifest-${{ github.run_id }}-${{ github.run_attempt }}`；run attempt 包含在名稱中，
rerun 不會覆寫既有 artifact。

Manifest policy/tool 由 selected SHA own：workflow 只執行 selected source root 中的
`infra/deploy/release_manifest.py`。Phase 3 foundation 之前，明確 `image_tag` 且已通過 protected-main
ancestor 驗證的 rollback 若 selected SHA 沒有此工具，會 warning 後保留既有 SHA-tag rollback，不產生
manifest artifact；此 legacy 分支只允許 `ECR_REUSE_ONLY=true`。foundation 之後的 selected SHA 缺少或
無法執行其自身工具一律 fail closed，不得把其他 manifest error 降級或 skip。
legacy rollback 的相容性檢查只涵蓋實際 staging host runtime contract：FinDB 的 Compose、三個
staging-rendered nginx inputs、三個 renderer 及 loader／command／host render／install／deploy／catalog，
Fetcher 的 loader／command／provider release／catalog。`serve-key.conf` 與
`render_nginx_serve_key.py` 非 staging 前置 input，故排除。純 CI ECR build helper 與 release-manifest tool
不在此集合，避免 foundation-only 差異阻斷 pre-foundation rollback；集合內任何檔案差異仍 fail closed。

Foundation live acceptance 已完成：Fetcher run
[33244760600](https://github.com/FPI-TW/findb/actions/runs/33244760600) 的三枚manifest digest經獨立SSM
command `56127a67-2eed-4535-a670-86faad915e21` 與host RepoDigest逐一比對，三個scheduler均running且
restart count 0；FinDB run [33245539837](https://github.com/FPI-TW/findb/actions/runs/33245539837)
的兩枚manifest digest經獨立SSM command `b6cb3d90-61c3-4a9f-bd80-3ad854dadd69`逐一比對，Alembic
`d6e7f8a9b0c1`、RabbitMQ、Serve、Ingest、Dashboard與Nginx皆健康。這些是artifact-to-live
RepoDigest一致性證據，不是digest deployment或accepted replay evidence。

實際 deployment 與 rollback 仍使用 SHA tag，manifest 尚未成為 deploy／rollback identity 或 accepted
manifest。staging build bridge現會把經repository/digest格式驗證的unit image refs傳至deploy job；bounded
SSM preflight會在任何SSH或writer interruption之前，使用instance role與tmpfs Docker config pull並inspect
所有exact digests。合併後自動化路徑已由共同merge SHA
`228989afe857c82d619cd53d15dbb29873d6710a`完成live驗收：FinDB run
[33246701516](https://github.com/FPI-TW/findb/actions/runs/33246701516) 的SSM command
`d6437246-f584-4545-9224-88867b8bdef9`為`Success`／exit 0並輸出
`ecr_digest_pull_inspect=ok images=2`；Fetcher run
[33246700446](https://github.com/FPI-TW/findb/actions/runs/33246700446) 的SSM command
`9e4596ca-1888-44bf-acef-89858f9b35d4`為`Success`／exit 0並輸出
`ecr_digest_pull_inspect=ok images=3`。兩個deploy與後續health／scheduler驗證均成功，兩份artifact亦由repo
validator重新驗證通過。Compose/helper digest cutover、private S3 accepted manifest與production promotion仍未完成；
因此不可勾選本 Phase exit gate。

- [x] Build jobs輸出每個image digest並建立manifest；FinDB記錄backend＋Dashboard，Fetcher記錄
  Twelve Data＋FinLab＋Shioaji；五個image reference均為完整staging ECR `repository@sha256:...`。
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
- [x] 在不中止服務的情況下，由SSM target pull並inspect所有exact digests；FinDB與Fetcher合併後live run
  已分別驗證兩枚與三枚exact digests。

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
- [x] Protected `main` 的 CD push path 僅完成同 revision CI 與 no-op bridge；staging 部署改由
  protected `main` 上的 manual dispatch 啟動，且不受Environment人工核准阻塞；required CI與PR
  protection 已完成外部驗證。
- [x] `infra/tofu/**`變更由IaC專用GitHub Actions在exact PR commit執行`fmt/init/validate/plan`；
  `staging-infra-plan` OIDC role不能修改受管資源或讀取runtime secret value，delete／replace
  fail closed，apply只可由protected `main`的fresh plan與獨立身分人工執行。
- [x] `staging-findb`與`staging-fetcher`有獨立OIDC deploy role、EC2 instance role與secret path。
- [x] 五個private staging ECR repositories均為immutable、AES-256、basic scan-on-push，且只有
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
- [ ] Runtime secrets不在persistent host env file，active catalog恰為17筆，兩筆空GHCR
  metadata已排程退役、兩個未使用PAT已撤銷；DB-backed rotation與RabbitMQ rotation均有evidence；provider
  與Raw／Canonical R2 scope項目依使用者核准變更acceptance criterion而完成，非rotation evidence。完整原生
  provider cycle gate通過後，另行取得移除授權、確認last-used與health並移除GitHub runtime copies。
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
