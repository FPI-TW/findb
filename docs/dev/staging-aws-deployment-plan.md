# Staging AWS Deployment Completion Plan

> 狀態：待執行。本文是 staging AWS 控制面、部署身分與驗收的核心成熟化計畫；現行可操作
> runbook 仍以 [`../operations/deployment.md`](../operations/deployment.md) 為準。
>
> 最後盤點：2026-08-21。Staging 目前正常運行，但「服務可用」不等同於部署身分、release
> 重播、secret 邊界與災難復原已完成。AWS、GitHub Environment 與 Cloudflare 的實際資源狀態
> 尚未由 repo 自動驗證，表中標示 `待盤點` 的欄位不得視為已完成。

## 決策背景

本計畫依下列已確認前提收斂，不以完整企業級治理作為 staging 完成條件：

- staging 是 production 的前置驗證站，預期三個月內開始建立 production；
- staging 由小團隊共同維運，不要求正式 24/7 on-call；
- protected `main` 合併後維持自動部署 staging，人工 Environment 核准留給 production；
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
  -> build images once and record exact digests
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
- staging HA、Auto Scaling、多 EC2、blue/green 或 Multi-AZ 強制要求；
- 全面 import 既有 AWS 資源到 IaC；
- 擴大 active feed universe、production-scale backfill 或新增資料來源；
- Canonical R2 publish/read runtime；
- 正式 24/7 on-call、完整企業稽核或 staging 每次部署的人工核准。

## 各階段必要性

| 階段 | 判定 | 原因與調整 |
| --- | --- | --- |
| Phase 0：盤點與保護 | 必要 | 確認實際 target、資料與復原 owner；不要求全面 IaC import，也不對 staging 加人工 deploy reviewer |
| Phase 1：OIDC、SSM、instance role | 必要 | 移除長效 SSH deployment identity，建立 service-specific AWS trust boundary |
| Phase 2：Runtime secrets | 必要 | 避免 runtime secrets 經 GitHub runner 與遠端 shell傳遞，並完成 credential rotation |
| Phase 3：Digest、manifest、CI gate | 必要 | staging 必須能重播並把相同 artifact promotion 到 production，不能依賴 `latest` 或可漂移 tag |
| Phase 4：FinDB SSM cutover | 必要 | 保留既有 migration 與 queue safety gate，只替換部署傳輸與secret來源 |
| Phase 5：Fetcher SSM cutover | 必要 | 保留 SQLite、Raw bucket binding 與 single-writer gate，只替換部署傳輸與secret來源 |
| Phase 6：必要維運基線 | 部分必要 | 完成關鍵告警、restore與broker rebuild；HA與完整on-call另案處理 |

## Repo 可證實的現況

| 項目 | 現況 | 核心基線仍缺少 |
| --- | --- | --- |
| Release units | FinDB 與 Fetcher 已有獨立 CI/CD、Environment 與 concurrency group | 將 AWS target、role、secret path 與 acceptance 寫入可稽核清冊 |
| CI gate | CD 以 `workflow_call` 執行同一 revision 的 CI | 受支援舊 revision migration upgrade、image runtime security 與 deploy bundle deterministic check |
| Image identity | Runtime 發布 commit SHA tag；FinDB 另發布 `latest`，Compose有`latest` fallback | 保存registry digest、以digest部署、移除所有`latest`部署路徑 |
| EC2 transport | `appleboy/ssh-action`與`scp-action`已固定完整Action SHA | OIDC、service-specific deploy role、SSM、target tag restriction與command log |
| Runtime secrets | GitHub Environment secrets逐一傳到遠端shell／container | Secrets Manager或SecureString、instance role、輪替與GitHub secret退場 |
| RDS rollout | 有predeploy DB check、writer pause、單一Alembic upgrade與revision check | RDS backup inventory、migration credential分權、restore rehearsal與release紀錄 |
| Queue | RabbitMQ在FinDB EC2，以EBS path保存；PostgreSQL是durable truth | 容量告警、broker全毀重建演練與實測恢復時間 |
| Fetcher state | 三個provider runtime隔離，SQLite state與Raw bucket binding有preflight | 一致性備份、SSM rollout與完整排程週期觀察 |
| R2 | Raw與Canonical bucket／credential契約已拆分 | 外部bucket policy/lifecycle盤點；Canonical runtime不得宣稱已通過資料面驗收 |
| Protection | workflow固定第三方Action SHA，repo要求PR | CODEOWNERS與branch／Environment protection的外部驗證紀錄 |
| Observability | 應用內已有health、queue與freshness checks | 關鍵CloudWatch alarms、log retention、告警接收者及synthetic test |
| IaC | Repo內尚無AWS IaC | 新控制面資源以團隊既有工具或OpenTofu納管；既有data-plane資源先盤點 |

目前 `.github/workflows/findb-cd.yml`、`.github/workflows/fetcher-cd.yml` 和
`docker-compose.prod.yml` 是現行行為的 source of truth。本文不把尚未查證的 AWS console
設定當作事實。

## Workflow / release unit matrix

| Unit | CI / CD boundary | Images | Environment | Concurrency | Deploy order |
| --- | --- | --- | --- | --- | --- |
| FinDB | Backend、Dashboard、contracts、Compose、nginx與FinDB workflows | Backend、Dashboard | `staging-findb` | `staging-findb`，`cancel-in-progress: false` | 先部署；contract變更維持backend-first |
| Fetcher | Fetcher、contracts、contract generation依賴與Fetcher workflows | Generic、FinLab、Shioaji | `staging-fetcher` | `staging-fetcher`，`cancel-in-progress: false` | FinDB acceptance後部署 |

兩個 CD 都由 protected `main` 的 path-filtered push 自動進入 staging，也可手動 dispatch；CD
必須直接呼叫同 revision CI。Staging Environment只允許protected branch，不設required reviewer；
production建立後才由其Environment設定required reviewer與prevent self-review。

FinDB仍是一個deployment unit，backend與Dashboard各自記錄digest。Fetcher三個image同屬一個
deployment unit，但release manifest必須列出三個digest，不能只用共同SHA tag代替。

## Staging 資源清冊

Phase 0 必須填入target-specific、非敏感的實際識別資料。Secret只記錄ARN或name，不得貼值、
hash或可比較片段。清冊完成前禁止停用現行SSH recovery path。

| 類別 | `staging-findb` | `staging-fetcher` | 驗收要求 |
| --- | --- | --- | --- |
| AWS account / Region | 待盤點 | 待盤點 | Workflow明確檢查STS account與region |
| VPC / subnet | 待盤點 | 待盤點 | RDS private；public EC2若保留須記錄例外與production前檢查點 |
| EC2 target / tags | 待盤點 | 待盤點 | `Project=FinDB`、`Environment=staging`、`DeploymentUnit=findb|fetcher`，每次恰好命中一台 |
| Instance role | 待建立／查證 | 待建立／查證 | 只讀自己的secret path與deploy bundle；可寫自己的log／metrics |
| GitHub deploy role | 待建立 | 待建立 | OIDC subject綁定對應Environment；不得讀runtime secrets |
| SSM managed node | 待盤點 | 待盤點 | Online、command output送unit-specific CloudWatch log group |
| RDS / security group | 待盤點 | 無權限、無網路路由 | `PubliclyAccessible=false`；只允許FinDB EC2 SG；backup/PITR啟用 |
| Secret prefix | `findb/staging/findb/` | `findb/staging/fetcher/` | 不共用DB、R2、provider、Source/Admin或deploy credential |
| Deploy artifact | Private、versioned control-plane S3 prefix | 同bucket的獨立prefix或獨立bucket | exact SHA key、checksum、versioning；不使用application R2 bucket |
| CloudWatch | 待盤點 | 待盤點 | Retention、owner與synthetic alarm結果有紀錄 |
| Backup | RDS、RabbitMQ EBS與必要static state待盤點 | SQLite/checkpoint EBS待盤點 | 完成對應restore／rebuild rehearsal並記錄實測時間 |
| Public endpoint | DNS/TLS待查證 | 無public inbound endpoint | 外部HTTPS readiness與certificate expiry可監控 |

## 身分、secret 與 release 設計

### GitHub OIDC 與 AWS roles

兩個deploy role的trust policy只接受GitHub OIDC provider，並同時限制：

```text
repository: FPI-TW/findb
aud: sts.amazonaws.com
sub (FinDB):  repo:FPI-TW/findb:environment:staging-findb
sub (Fetcher): repo:FPI-TW/findb:environment:staging-fetcher
```

實作前以實際OIDC claim確認owner大小寫。只有deploy job取得`id-token: write`；CI與build job
不得取得AWS身分。Deploy role只可查驗target、上傳release artifact及對對應tag的唯一EC2執行
SSM command，不得讀Secrets Manager value、RDS data、R2 credential或另一unit的target。

EC2 instance role負責讀取自身runtime secrets與deploy bundle。FinDB與Fetcher的deploy role、
instance role、secret path及KMS policy scope不得重用。

### Runtime secrets

預設使用Secrets Manager保存需要版本與輪替紀錄的RDS、provider、R2、DB-backed API key、
RabbitMQ及GHCR pull credential；簡單且低頻變更的敏感值可使用SSM SecureString。非敏感設定留在
GitHub Environment variables或Parameter Store一般參數。

至少維持下列邊界：

- RDS migration credential不得常駐注入application containers；
- RDS application、Canonical publisher與Canonical reader依用途分離；
- Fetcher shared、各provider與Raw R2 credential只供對應consumer；
- GHCR若維持private，使用package-read-only credential並由instance role載入，不再轉送workflow
  `GITHUB_TOKEN`；若未來packages改為public，移除此credential；
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
  "images": {"name": "registry/repository@sha256:..."},
  "migration_revision": "alembic revision or none",
  "contract_versions": ["versioned contracts"],
  "deployment_bundle_sha256": "...",
  "created_by_run_id": "..."
}
```

- Build job從registry取得每個image digest；SHA tag只作索引，不作部署或rollback identity；
- Compose與Fetcher deploy helper只接受完整`image@sha256:...`，缺值或`:latest`一律fail closed；
- Deploy bundle使用private、versioned的AWS S3 control-plane bucket，與Raw／Canonical R2分離；
- Host在停止writer前先驗證manifest、bundle checksum、target、pull exact digests並render config；
- 成功後保存accepted manifest、Alembic revision、SSM command ID與acceptance結果；
- 未來production promotion只能使用staging已接受的相同digests，不重新build。

## IaC 邊界

若團隊已有統一Terraform／OpenTofu工具與remote state，沿用該標準；否則採OpenTofu與encrypted
remote state。這次只納管新建或為cutover明確修改的控制面資源：

- GitHub OIDC provider引用、FinDB／Fetcher deploy roles與instance roles；
- SSM、KMS／secret policies、CloudWatch log groups及必要alarms；
- private、versioned deploy bundle／release manifest S3 bucket與權限。

既有EC2、RDS、VPC、subnet、security groups、R2與DNS先用read-only data source或資源清冊引用。
若實作中發現已有外部IaC owner，先import或交由原owner修改，禁止建立第二套同名資源。全面納管
既有AWS data plane另案進行，不阻塞本次staging hardening。

## 實作波次

每一波各自提PR；先完成read-only或dual-path驗證，再移除舊路徑。不得在同一個deployment同時
切換OIDC、secret來源、image identity與migration流程。

### Phase 0：盤點與變更保護

- [ ] 填完staging資源清冊，記錄owner、AWS account/region、resource ARN/ID、資料分類、backup
  policy、告警接收者與公開網路例外。
- [ ] 驗證`main`的required CI與PR protection；`staging-findb`、`staging-fetcher`只允許protected
  branch，但不設required reviewer，維持main合併後自動部署。
- [ ] 建立`.github/CODEOWNERS`，至少覆蓋workflows、`infra/**`、Compose、migrations與contracts。
- [ ] 依IaC邊界確認團隊既有工具與state owner；沒有既有標準時採OpenTofu，只納管新控制面。
- [ ] 記錄目前accepted SHA、實際image identity、Alembic revision、running containers、RDS
  snapshot／backup狀態與SSH recovery owner。

Exit gate：不存在未知的target、database、secret或backup owner；目前SSH部署仍可用；staging自動
部署不會被Environment reviewer阻塞。

### Phase 1：OIDC、SSM與instance role基礎

- [ ] 建立或引用GitHub OIDC provider，建立兩個staging deploy roles，trust綁定各自Environment。
- [ ] 為兩台EC2建立獨立instance profile、必要target tags、SSM agent與Session Manager設定。
- [ ] SSM command output送到unit-specific CloudWatch log group，設定retention且禁止secret輸出。
- [ ] 以OIDC執行無副作用preflight：STS account／role、target count恰為1、environment marker、
  Docker／Compose版本、disk／inode、time sync、DNS與instance profile。
- [ ] SSM穩定前保留受限SSH recovery，不在本Phase提前移除TCP/22。

Exit gate：兩個Environment只能命中各自一台EC2；cross-unit SSM與secret read均被IAM拒絕；
Session Manager recovery可用。

### Phase 2：Runtime secrets遷移

- [ ] 依consumer邊界建立target-specific secrets與KMS policy；先寫入新版本，不刪GitHub值。
- [ ] 新增host-side secret loader，以allowlist取值、寫入tmpfs、驗證owner／mode並於結束後清理。
- [ ] 先用現行SSH CD完成一次非資料產生的deploy，改由instance role取secret，確認每個container
  只取得必要credential。
- [ ] 逐一輪替DB-backed keys、R2、provider、RabbitMQ與GHCR credentials，觀察完整排程週期。
- [ ] 確認last-used與health後，撤銷GitHub Environment及persistent host env中的舊runtime secrets；
  GitHub只保留role ARN、region、target selector、public host與secret identifier等非敏感值。

Exit gate：deploy role無法讀secret value；workflow log與SSM command不含runtime secret；舊
credential已撤銷而非只複製；FinDB與Fetcher無cross-secret read。

### Phase 3：Digest、release manifest與CI gate

- [ ] Build jobs輸出每個image digest並建立manifest；FinDB記錄backend＋Dashboard，Fetcher記錄
  generic＋FinLab＋Shioaji。
- [ ] 建立versioned deploy bundle，納入Compose、nginx templates與deploy helper checksum。
- [ ] Compose與deploy helper改為必填完整image reference；移除FinDB `latest`發布及所有fallback。
- [ ] CI驗證manifest schema、SHA／digest格式、bundle checksum、deterministic generation與config
  render，缺值或`:latest`一律失敗。
- [ ] 在空資料庫與所有受支援舊revision fixture執行migration upgrade，並驗證單一Alembic head。
- [ ] 驗證container entrypoint、health command、non-root、read-only root filesystem、drop
  capabilities與必要writable paths；不相容項目須修正或記錄具體、具期限的例外。
- [ ] 在不中止服務的情況下，由SSM target pull並inspect所有exact digests。

Exit gate：相同manifest可重播且不重新build；tag漂移不影響部署；CI可攔截不安全的image、
manifest與migration。

### Phase 4：FinDB改走SSM

- [ ] 將FinDB deploy job改成OIDC＋SSM；停止workflow傳送runtime secrets並移除SSH／SCP action。
- [ ] Preflight確認RDS TLS、revision、connection headroom、long transaction、backup／PITR與磁碟。
- [ ] 先停`ingest`、`dispatcher`、`worker`、`raw-cleanup`及所有DB writers，再以migration credential
  執行單一Alembic job；`serve`只在schema相容時保留。
- [ ] 啟動candidate後驗證container、internal health、public TLS／readiness、queue topology、
  worker ping、DB queue health與bounded DB transaction。
- [ ] 保存accepted manifest與SSM command ID；演練application-only previous digest rollback，並
  驗證schema不相容時拒絕回切、writers保持停止。

Exit gate：連續兩次FinDB staging deploy與一次rollback rehearsal不使用SSH；migration失敗不會
啟動新writers；`FINDB_EC2_*` secrets已移除。

### Phase 5：Fetcher改走SSM

- [ ] 將Fetcher deploy與FinLab smoke改成OIDC＋SSM；停止workflow傳送provider、R2與Source
  credentials並移除SSH action。
- [ ] 保留provider-specific state owner／mode、SQLite quick-check、Raw bucket marker與
  single-writer reconciliation全部現行gate。
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
- [ ] FinDB與Fetcher各完成兩次SSM deployment且Session Manager recovery驗證成功後，移除兩台
  EC2的SSH ingress、GitHub SSH secrets與未使用key pair，保留SSM break-glass流程與audit trail。

Exit gate：SSM是唯一日常部署與管理路徑；關鍵alarm、RDS restore、SQLite recovery與broker
rebuild都有最近一次成功紀錄；SSH deployment identity已撤銷。

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

- [ ] 四個GitHub workflow的path、CI、Environment、concurrency與release unit matrix一致。
- [ ] Protected `main`自動部署staging，不受Environment reviewer阻塞；高風險檔案已有CODEOWNERS。
- [ ] `staging-findb`與`staging-fetcher`有獨立OIDC deploy role、EC2 instance role與secret path。
- [ ] 日常deploy不使用SSH／SCP，EC2不開放SSH ingress，cross-unit IAM測試fail closed。
- [ ] 所有application image以digest部署，accepted release manifest可重播並供production promotion。
- [ ] Runtime secrets不在GitHub或persistent host env file，並完成一次實際輪替。
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
| 全面AWS IaC import | 延後；本次只納管新控制面 | 現有resource owner確認、準備進行production環境複製或drift治理 |
| Canonical R2資料面驗收 | 延後；只完成bucket與credential邊界 | Canonical publish/read/sign runtime完成 |
| 正式24/7 on-call與企業稽核 | 延後；使用具名owner與通知channel | Production SLA、法遵或客戶稽核需求確立 |
| Staging Environment人工核准 | 不採用；維持protected main自動部署 | Staging開始承載高敏感資料或不可逆外部副作用 |

這份檔案是暫時性執行計畫，不是永久runbook。不得在尚有未完成Phase、未搬移的操作知識或
只存在本文的驗收證據時提前刪除；全部完成並完成文件搬移後，也不應繼續保留本計畫。
