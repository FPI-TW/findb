# Production foundation 與 cutover

Production 固定使用 AWS account `289112218471`、region `ap-southeast-1`，state、KMS、
ECR、S3、IAM、DB、host、runtime secret path 與 staging 完全隔離。此文件是執行契約；PR 中
的 plan 或資源宣告不等於 live acceptance。

## Foundation apply

1. 使用 `findb-production` profile 驗證 caller identity，先在
   `infra/tofu/production-bootstrap` 執行 `fmt`、`validate`、refresh plan。Bootstrap plan 只能
   建立 state KMS 與 `findb-production-tofu-state-289112218471`，不可出現 delete。
2. 經核准後 apply bootstrap，將其 local state 遷移至獨立的
   `production/bootstrap.tfstate`；確認 versioning、public access block、SSE-KMS 與 lockfile。
   遷移前先將 `production-bootstrap/backend.s3.tf.example` 複製為 ignored `backend.tf`，再使用
   `bootstrap_backend_migrate_command` output；確認 remote state 完整後才安全刪除 local state。
3. 以 bootstrap output 的 KMS ARN初始化 `infra/tofu/production`。保存 refresh plan，確認
   account output 為 `289112218471` 且沒有 delete，再由受信任 operator apply。
4. 同一 PR 對 `infra/tofu/staging` 執行 refresh plan，新增的兩個 promotion-reader role 之外
   必須 zero-delete。Reader 只可讀自己 unit 的 `*/accepted/`、acceptance record 與 ECR layers。

禁止從 PR runner apply；禁止在 state bucket 存 application secret；禁止把 bootstrap saved plan
拿來 apply foundation。任何 account、region、resource prefix 或 delete 不符即停止。

## Live foundation acceptance

- VPC CIDR 必須是 `10.20.0.0/16`，無 NAT；RDS subnets 無 internet route。
- FinDB `t3.large`、Fetcher `t3.medium`，IMDSv2 required、hop limit 2、無 key pair、無 TCP 22。
  FinDB 443 只接受 Cloudflare published CIDRs；Fetcher security group 無 ingress。
- RDS 必須為 private PostgreSQL 16、`db.t4g.medium`、100 GiB gp3、KMS、Single-AZ、14 日
  backup、deletion protection，5432 僅接受 FinDB security group。
- 五個 ECR repository 必須 immutable 且 scan-on-push；instance role 只 pull 自己 unit，deploy
  role不能讀 Secrets Manager values，promotion role 只寫自己 unit 的 ECR 與 bundle metadata。
- 兩台 host 必須出現在 SSM managed nodes；使用 bounded SSM command 驗證 instance role、unit
  ECR pull、unit bundle read、cross-unit denial、runtime secret isolation與 RDS connectivity。
- 驗證 CloudTrail multi-region/log validation、GuardDuty、DLM recovery point、RDS automated backup、
  CloudWatch alarms 與寄至 `tyler.ho@fpitw.com` 的 SNS subscription。Custom metrics 在 collector
  尚未健康發布時必須因 missing data 告警，不得把 `INSUFFICIENT_DATA` 當通過。
- 所有資源必須有 `Project=findb`、`Environment=production`、`DeploymentUnit`、`Owner`、
  `BackupOwner`，並在 Cost Explorer 啟用這些 cost allocation tags；不建立固定 Budget。

## External resources 與 secrets

Cloudflare 帳號中建立 private `findb-production-raw` 與 `findb-production-canonical` R2 buckets。
Raw lifecycle 30 日並保護前 7 日；Fetcher raw credential只可讀寫 raw。Canonical publisher只可
讀寫 canonical，Serve reader只可讀 canonical，三組 access key/secret不得相同。管理 credential
不得注入 runtime。

建立 proxied `findb.tingfong.com` A record 指向 FinDB EIP，origin 使用獨立 Cloudflare origin
certificate。Fetcher EIP 的 `/32` 必須成為 `SOURCE_ALLOWLIST_CIDRS`。憑證、R2、DB、RabbitMQ、
四類 API key 與三個 provider credential 全部重新產生，分別寫入 OpenTofu 已建立的
`findb/production/findb/*` 或 `findb/production/fetcher/*` metadata；不得重用 staging 值。
Origin certificate secret固定使用單行 base64 欄位
`FINDB_ORIGIN_CERTIFICATE_PEM_B64`與`FINDB_ORIGIN_PRIVATE_KEY_PEM_B64`；部署只在instance上解碼、
驗證hostname、至少30日效期與keypair一致性，再原子寫入`/run/findb-runtime-secrets/nginx/`。
憑證與私鑰不得持久化到`/home/ubuntu`或進入GitHub Environment。

Source client scopes固定為：

- `twelve_data`：`us_equity_eod`
- `finlab`：`tw_equity_eod`
- `shioaji`：`tw_equity_minute`、`tw_etf_minute`

Secrets Manager 寫值與 Cloudflare credential 建立都屬獨立敏感操作，必須在 foundation live
acceptance 後執行並留下不含 secret 的 fingerprint/evidence。

## DB bootstrap 與 Environment gate

Migration後只 seed dataset registry、已審核 US calendar、TWSE 已正式發布的 2025–2026 calendar
revisions、獨立 DB-backed credentials與三個 scheduler controls。三個 scheduler 的
desired/observed state都必須為`stopped`；不可複製staging canonical/raw/workflow data。
2027以後尚未發布的TW calendar不阻塞首次部署；正式資料發布後必須以PR加入、完成checksum review
並在對應年度開始前發布新revision。Production feed不得以weekday推測取代官方資料，執行日期超出
目前reviewed coverage時必須fail closed。

由 OpenTofu outputs填入兩份 ignored `infra/env/production/*/.env.remote`，先執行 sync script的
dry-run，再建立 branch policy僅允許 `main` 的 `production-findb` 與 `production-fetcher`。兩個
Environment 只存 control-plane variables，不存 application secrets。只有下列 gate 全通過才可另外
建立 `PRODUCTION_DEPLOY_ENABLED=true`：

- foundation、host、RDS、SSM、ECR、secret isolation、DNS/TLS、R2、monitoring/backup通過；
- exact protected-main SHA 的 FinDB/Fetcher staging rollout均有 v2 accepted bundle；
- staging promotion-reader可讀自身 accepted bundle/ECR且 cross-unit denied；
- Production `.env.remote` dry-run無 placeholder、缺值或額外 key。

任一 live gate失敗，移除或將 gate設為非 `true`，所有 scheduler恢復 `stopped`。不得 fallback至
SHA tag、staging bundle、舊 image或 rebuild；Production只部署 `repository@sha256`。
