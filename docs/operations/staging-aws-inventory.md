# Staging AWS Resource Inventory

> 本文件保存 staging 部署控制面的非敏感資源識別、已驗證狀態、owner與例外。
> Secret只記錄用途或名稱，不記錄值、hash或可比較片段。操作流程仍以
> [`deployment.md`](deployment.md) 為準。

最後唯讀盤點：2026-08-21。Owner若未另列，暫由`@tylercore`負責。EC2內容已完成唯讀查驗；
AWS API/Console session在本次盤點不可用，因此標示「AWS API待驗證」的RDS、EBS snapshot、
IAM、tag與CloudWatch欄位不得視為已完成。

## GitHub control plane

| 項目 | 已驗證現況 | 缺口 / acceptance |
| --- | --- | --- |
| `staging-findb` | 只允許`main`；16 secrets、20 variables | 符合單人維護政策；持續將契約檢查整合進pipeline |
| `staging-fetcher` | 只允許`main`；13 secrets、15 variables | 符合單人維護政策；持續將契約檢查整合進pipeline |
| Organization ruleset | `Main protection`為Active，套用default branch；禁止刪除、必須PR、禁止force-push | 維持現況；不要求核准數 |
| PR settings | 新commit會dismiss stale approval；允許merge、squash、rebase | Phase 0需決定是否限制merge method |
| Classic protection | 未設定 | 組織ruleset是現行source of truth，不需重複建立classic rule |

目前採單人維護模式，其餘人員只在主要維護者請假時代理，因此不增加強制審核規則。這是
刻意的治理例外，不是Phase 0 blocker。補償控制是所有`main`更新都經PR、禁止force-push、
Environment只接受`main`、Action固定SHA、CD重跑同revision CI，以及將重複的環境契約、
migration、artifact與部署驗收逐步變成fail-closed pipeline檢查。

Environment現有runtime secrets仍包含EC2 SSH、RDS、RabbitMQ、DB-backed API keys、provider
與R2 credentials；這是Phase 2前的過渡狀態。Repo或文件不得保存這些值。

### Environment contract drift

- `staging-fetcher`仍有已退休且程式未使用的`FETCHER_CALENDAR_MODE` variable。
- `staging-fetcher`未設定`FETCHER_SCHEDULER_CONTROL_POLL_SECONDS`，workflow目前使用明確的
  `30`秒fallback；下次Environment sync前應補回契約值。
- 本機ignored `infra/env/staging/fetcher/.env.remote`在盤點時含重複的Raw R2 assignment
  names；值未被讀出或記錄。同步工具必須先拒絕重複key，operator再人工保留正確的一筆。

## AWS target inventory

共同邊界：

| 欄位 | 值 | 證據 |
| --- | --- | --- |
| AWS account | `439622209937` | EC2 IMDSv2 instance identity document |
| Region | `ap-southeast-1` | EC2 IMDSv2 |
| VPC | `vpc-0865afcf10442bf4d` | EC2 IMDSv2 network metadata |
| Topology | FinDB與Fetcher各一台EC2；同VPC、不同AZ/subnet | 兩台EC2 IMDSv2 |
| Availability | single-instance staging，不宣稱HA | runtime inventory |

### FinDB target

| 欄位 | 已驗證值 / 狀態 |
| --- | --- |
| Deployment unit | `staging-findb` |
| EC2 instance | `i-0942016913367a8b2`，`m7i.large`，`x86_64` |
| AMI | `ami-0c687e8f5c4e54af5` |
| AZ / subnet | `ap-southeast-1c` / `subnet-0aba2a175b5912c24` |
| Security groups | `sg-0194615fe18784889`、`sg-070694f093a25cb31`；rules待AWS API驗證 |
| Private IP | `172.31.14.185` |
| Instance profile | none |
| SSM agent | installed但`inactive` |
| Docker / Compose | `29.2.1` / `5.1.0` |
| Root EBS | `vol-0784675e2ada6642e`，50 GiB；RabbitMQ與generated cache共用root volume |
| Disk baseline | 49 GiB filesystem，約16% used |
| Public management | 現行SSH可達；ingress CIDR與EIP association待AWS API驗證 |

### Fetcher target

| 欄位 | 已驗證值 / 狀態 |
| --- | --- |
| Deployment unit | `staging-fetcher` |
| EC2 instance | `i-05f518ef183bc31a9`，`t3.small`，`x86_64` |
| AMI | `ami-0532913178263be11` |
| AZ / subnet | `ap-southeast-1a` / `subnet-0cde9e8dc33bec41e` |
| Security group | `sg-0c98f59c6961a00d2`；rules待AWS API驗證 |
| Private IP | `172.31.38.185` |
| Instance profile | none |
| SSM agent | installed但`inactive` |
| Docker / Compose | `29.6.2` / `5.3.1` |
| Root EBS | `vol-0201fa5249fe44c39`，30 GiB；三個provider SQLite/checkpoint共用root volume |
| Disk baseline | 29 GiB filesystem，約24% used |
| Public management | 現行SSH可達；ingress CIDR與public address association待AWS API驗證 |

兩台instance目前都沒有可由IMDS取得的instance tags。Phase 1建立SSM target selector前，必須
透過AWS API確認或補上：

```text
Project=FinDB
Environment=staging
DeploymentUnit=findb|fetcher
Owner=tylercore-or-approved-team
```

## RDS inventory

| 欄位 | 已驗證值 / 狀態 |
| --- | --- |
| Endpoint | `fin-db.cfq8i4ck82we.ap-southeast-1.rds.amazonaws.com:5432` |
| Database | `findb` |
| Engine observation | PostgreSQL `16.13`，primary (`pg_is_in_recovery() = false`) |
| Network observation | 從FinDB container解析為private `172.31.48.118` |
| Alembic revision | `c5d6e7f8a9b0 (head)` |
| Cluster/instance ARN與resource ID | AWS API待驗證 |
| DB subnet group / security groups | AWS API待驗證；acceptance要求只允許FinDB EC2 SG |
| `PubliclyAccessible` | AWS API待驗證；acceptance必須為`false` |
| Encryption / KMS key | AWS API待驗證 |
| Automated backup / PITR / retention | AWS API待驗證 |
| Deletion protection / Multi-AZ | AWS API待驗證 |
| Latest restorable time / snapshot | AWS API待驗證；Phase 0 exit gate blocker |
| Restore rehearsal / RPO / RTO | 尚無可查驗紀錄；Phase 6前必做 |

## R2 inventory

| 用途 | Environment | Account | Bucket | Runtime scope |
| --- | --- | --- | --- | --- |
| Raw | `staging-fetcher` | `ef6190725fbf3a4331203b901a2d2961` | `findb-staging-raw` | Fetcher Object Read & Write |
| Canonical | `staging-findb` | `ef6190725fbf3a4331203b901a2d2961` | `findb-staging-canonical` | Worker publisher R/W、Serve reader R/O |

Bucket private/public policy、lifecycle、lock與token scope仍需Cloudflare控制面驗證。Canonical
runtime尚未實作，不能把credential wiring記為資料面acceptance。

## Accepted runtime baseline

盤點時`main`為`029cf6ed74e22fdb6b30d593771c01e827f4a58d`（文件PR #152）；實際
staging runtime仍是上一個程式release：

```text
accepted_sha=7915ea856b677a67776476cd87cb531617203ed0
alembic_revision=c5d6e7f8a9b0
```

| Unit | Runtime baseline |
| --- | --- |
| FinDB | backend與Dashboard均為上述SHA tag；serve、ingest、worker、Dashboard、nginx與RabbitMQ回報healthy，dispatcher/raw-cleanup為running |
| Fetcher | generic、FinLab、Shioaji三個scheduler均為上述SHA tag且running |
| Artifact limitation | 只記錄tag，沒有accepted image digest、bundle checksum或release manifest |

## Phase 0 architecture decisions

以下決策是後續實作預設；變更時需在同一PR更新本文件與deployment plan：

| 決策 | Phase 0選擇 | 邊界 |
| --- | --- | --- |
| IaC | OpenTofu，放在`infra/aws/`；先import現有staging資源再允許apply | 版本與remote-state bootstrap在首個IaC PR固定；禁止console與IaC平行建立同名資源 |
| IaC state | 專用、encrypted、versioned S3 state bucket與locking，不與deploy bundle或application data共用 | backend resource與recovery owner需先建立並記錄 |
| Deploy bundle | 專用private/versioned S3 control-plane bucket，unit-specific prefix | 不使用Raw/Canonical R2；GitHub deploy role只寫自身prefix，instance role只讀exact key |
| Runtime secret | Secrets Manager；非敏感、低頻設定可用Parameter Store String | RDS/provider/R2/API/RabbitMQ/GHCR credential不得用普通Parameter String |
| KMS | FinDB與Fetcher使用不同key或至少不可跨讀的獨立key policy | deploy role不可decrypt runtime secrets |
| Target selection | EC2 tags＋SSM managed node，且每個unit deployment恰好match一台 | instance ID只作inventory，不作無限制fallback |
| Repository governance | 單人維護；代理人只在請假時介入 | 不增加強制審核規則；若出現第二位固定維護者、production風險提高或稽核要求變更再重評估 |
| Resource owner | 暫定`@tylercore` | 建立正式infra/on-call責任分工後再更新owner |

## Open risks and Phase 0 exit blockers

| 風險 / 缺口 | Owner | 退場期限或觸發條件 | 補償控制 |
| --- | --- | --- | --- |
| SSH/SCP與GitHub runtime secrets | `@tylercore` | 分別在Phase 1/2 acceptance後移除 | Environment main-only、service分流、Action固定SHA |
| 兩台EC2無instance profile、SSM inactive | `@tylercore` | Phase 1 exit前 | 目前保留SSH recovery；不得提前關閉唯一管理路徑 |
| EC2 security-group ingress未由AWS API驗證 | `@tylercore` | Phase 0 exit前 | 不擴大現有規則；取得AWS read-only身分後立即audit |
| RDS backup/PITR/deletion protection未知 | `@tylercore` | 任何migration或Phase 0 exit前 | 不執行新的高風險migration |
| RabbitMQ與Fetcher state共用root EBS且snapshot policy未知 | `@tylercore` | Phase 6 exit前；production規劃前必須rehearse | PostgreSQL outbox是queue truth；Fetcher維持single writer與SQLite preflight |
| 單EC2 failure domain | `@tylercore` | production topology決策前重評估 | staging不宣稱HA；保留bounded acceptance |
| Environment contract drift與本機duplicate keys | `@tylercore` | 下次Environment sync前 | sync工具對duplicate key fail closed；不自動刪除remote設定 |

## 證據取得與人工確認邊界

未另行核准前，EC2只執行唯讀查驗。可自動確認的範圍包括IMDS identity/network、OS、
Docker/Compose、磁碟、container/image/health、SSM agent與instance profile狀態，以及從既有
FinDB container觀察RDS endpoint、PostgreSQL版本與Alembic revision。

下列項目無法只由EC2可靠證實，Phase 0必須逐項列給人工透過AWS、GitHub或Cloudflare控制面
確認；確認前維持「待驗證」，不得推測、變更或建立替代資源：

- EC2 tags、ENI/public address/EIP、subnet route table、security-group ingress/egress與SSH來源CIDR。
- EBS volume encryption/KMS、snapshot、AWS Backup plan、retention與restore紀錄。
- RDS ARN/resource ID、subnet/security groups、public access、encryption/KMS、backup/PITR、
  retention、deletion protection、Multi-AZ、latest restorable time與snapshot。
- IAM instance profile、OIDC provider、deploy/instance role與policy；SSM managed-node/control-plane
  狀態；CloudWatch log groups、retention、alarms、通知target與alarm test。
- R2 bucket private/public policy、lifecycle、lock與token scope。
- GitHub Environment/organization ruleset的控制面設定，以及任何secret/variable新增、修改或移除。

人工查核需保存查核日期、操作者與非敏感結果。不得用deploy/admin credential代替read-only
盤點身分，也不得在命令、截圖或文件輸出secret value。
