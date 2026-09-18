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
  FinDB 443 只接受 Cloudflare published CIDRs；Fetcher security group 無 ingress。兩台host都必須安裝
  Docker Engine與Compose v2；`docker compose version`是部署前必要檢查。
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
憑證與私鑰不得持久化到`/home/ubuntu`或進入GitHub Environment。部署產生的三個非秘密nginx
設定檔必須在驗證前正規化為root-owned `0644`；tmpfs中的憑證、私鑰與lookup key仍維持`0600`。

Source client scopes固定為：

- `twelve_data`：`us_equity_eod`
- `finlab`：`tw_equity_eod`
- `shioaji`：`tw_equity_minute`、`tw_etf_minute`

Secrets Manager 寫值與 Cloudflare credential 建立都屬獨立敏感操作，必須在 foundation live
acceptance 後執行並留下不含 secret 的 fingerprint/evidence。

## DB bootstrap 與 Environment gate

Migration後只 seed四個正式dataset registry declarations、已審核的NYSE 2025–2028 calendar、
TWSE已正式發布的2025–2026 calendar revisions、獨立DB-backed credentials與三個scheduler
controls。三個 scheduler 的
desired/observed state都必須為`stopped`；不可複製staging canonical/raw/workflow data。
2027以後尚未發布的TW calendar不阻塞首次部署；正式資料發布後必須以PR加入、完成checksum review
並在對應年度開始前發布新revision。Production feed不得以weekday推測取代官方資料，執行日期超出
目前reviewed coverage時必須fail closed。

首次部署的 predeploy gate 只在`DEPLOYMENT_TARGET=production`接受完全沒有任何使用者 relation 的
乾淨資料庫；candidate acceptance落盤後，activation才可執行唯一一次`alembic upgrade head`。
Alembic完成後、任何常駐服務啟動前，activation必須在同一個one-shot migration secret scope校準並
驗證application DB role的最小權限；只允許`public`與`raw`的schema `USAGE`、table DML及sequence
使用權，不得授予schema `CREATE`或把migration credential帶入常駐container；Alembic revision table
是例外，只允許application role讀取，不得修改。
同一個bootstrap階段也必須將Secrets Manager的queue-health Admin viewer、Dashboard lookup Serve與
static-cache Serve三把key以hash校準成DB-backed machine credentials，確保readiness與cache probe不
依賴break-glass key；plaintext不得寫入DB或log。停止writers前必須先在application role transaction
執行`--check-only`並rollback；production既有的`production queue health`、`production lookup`與
`production static cache`只有在各自hash、kind、active與不過期條件全數符合時才能原地採用為
canonical deployment identities，其它identity reuse一律fail closed。
Fetcher-facing DB credentials由另一個明確的hashed operator bridge校準：受信任operator讀取
`fetcher` unit的三把Source key與calendar Serve key，只把lowercase SHA-256傳給FinDB host上的
`reconcile_fetcher_credentials.py`。FinDB與Fetcher instance roles仍必須互相拒絕讀取對方Secrets
Manager values；不得為了自動化bootstrap放寬cross-unit IAM。腳本只輸出identity、action與短
fingerprint，並將Source allowlist固定為`twelve_data/us_equity_eod`、`finlab/tw_equity_eod`及
`shioaji/{tw_equity_minute,tw_etf_minute}`，calendar key固定為`serve` scope及page size 1000。
只要已存在任一 table、partition、view、materialized view、sequence或foreign table卻沒有Alembic
revision，即使帶有bootstrap旗標也必須fail closed。Staging與後續已有revision的Production部署維持
原本的相容性／exact revision檢查，不得以手動migration、staging image或跳過preflight繞過。
SSM host preflight通常要求至少一個目標服務容器正在執行；只有Production首次部署可在Docker完全沒有
任何既有或停止容器時通過乾淨host例外。Staging、已有任一容器的Production host，或服務容器全數
停止但仍有殘留容器時都必須fail closed；此例外不放寬後續的exact digest、secret isolation與乾淨DB
檢查。Secret isolation probe固定讀取另一unit已建立的`runtime/configuration`名稱並要求失敗，隨後
必須成功載入本unit catalog；不得以不存在的同unit假secret或僅比對AWS CLI錯誤文字取代。

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

## Initial production deployment evidence（2026-09-18）

Release commit `916e077359efc874d4a8fd8bed6f0cb173fc16ed`的FinDB與Fetcher promotion runs
[35247678383](https://github.com/FPI-TW/findb/actions/runs/35247678383)、
[35291487946](https://github.com/FPI-TW/findb/actions/runs/35291487946)均成功。FinDB public health、
Dashboard、lookup Referer injection、Alembic、RabbitMQ topology、worker ping及DB-authoritative queue
health皆通過；Fetcher三個scheduler與三個historical containers均為accepted exact-digest、running且
restart count 0。三個scheduler controls後驗仍為`desired=stopped`／`observed=stopped`，沒有因部署
自動開啟production資料取得。兩個production Environment gate均已恢復為`false`。
SSM command `deb08244-8078-4959-a90d-144cee600dbf`另以migration identity撤銷application role對
`public.alembic_version`的寫入權限，後驗為可`SELECT`且不可`INSERT`／`UPDATE`／`DELETE`。

## Production v0.1.2 deployment evidence（2026-09-18）

Release commit `b7a520c0df325d4726d0bd5368e111bb7b027940`以`findb-v0.1.2`與
`fetcher-v0.1.2`兩個immutable tags依序完成promotion。FinDB run
[35302455968](https://github.com/FPI-TW/findb/actions/runs/35302455968)與Fetcher run
[35303227094](https://github.com/FPI-TW/findb/actions/runs/35303227094)均成功，部署後兩個
`PRODUCTION_DEPLOY_ENABLED` Environment gates已立即恢復為`false`。

FinDB production acceptance bundle SHA-256為
`340d9f2cd37d6f20686356db57c7716808248f960cd80cfb40474ac0164db67c`，active release使用backend
`sha256:0933e093588aff68a015d278da188186a945fa5b96e46cbd17e64b38606da610`與Dashboard
`sha256:54117506170b530f0eb6fc54f4ef325f31b508db16c462a24d29fb1e3c992576`，Alembic revision為
`a8b9c0d1e2f3`。Public health、Dashboard、lookup redirect與Referer注入的Serve查詢均通過；SSM
command `8e0cfdb0-f285-4114-8410-0b6028502455`確認8個FinDB containers正常，active symlink指向本次
accepted release。

Fetcher production acceptance bundle SHA-256為
`506bf8427194ded00e71c2836ea4ab525dfa5d24a0776bbc1d5675ad8bc3c46f`。Twelve Data、FinLab與
Shioaji分別使用`sha256:5b80c0a88f24f6c05285734ef74e96c09993fcff137b1c8168e171b732b2ac9e`、
`sha256:1725611d326a2c0b7b878093241330f8dea5ad56c87b7ad995cb54e1912a9288`及
`sha256:67b6b6ca6344330c7da7891e3a21b06001a37221088ec51dc8555b91219d84e3`。SSM command
`0f840f66-3223-4f4c-89d9-0661963e1105`確認三個stable containers皆為accepted exact-digest、running、
restart count 0，三個historical containers亦為running；command
`fef787dc-18ce-4782-8d47-95ad5805cd4e`確認三個scheduler controls均維持
`desired=stopped`／`observed=stopped`、heartbeat新鮮且沒有error。部署沒有啟用任何provider。

部署後的market-freshness驗收另發現三張Scheduler卡片皆顯示「設定錯誤」。Runtime controls本身正常；
FinLab與Shioaji的錯誤是production DB沒有任何published calendar revision，Twelve Data則是fresh-install
產生的`us_equity_eod.delivery_expectation`只有schedule與missing-delivery設定，缺少`latest_date`等完整
policy。根因是activation以`bash -s`從stdin執行migration區塊，第一個`docker compose run`繼承並消耗
剩餘stdin，令後續privilege／credential reconciliation、四feed registry provisioning與calendar seed
靜默跳過，但外層shell仍以0結束。Application與migration URLs已去敏確認指向同一個production RDS
database，排除寫入錯誤DB。

同次forward修正為每個migration one-shot明確使用`</dev/null>`、以可重現stdin消耗的測試保證五個
commands完整執行，並把registry provisioning擴充為原子校準四個正式feed：contract declaration採版本
控制值、operator-owned policy overrides保留、每個source都必須具有可解析的`latest_date`。Production
calendar seed同時發布repo內已審核的NYSE 2025–2028與TWSE 2025–2026完整年度revision；任一既有
revision與reviewed source不同即fail closed。修正部署驗收必須再次確認三張卡片的
`configuration_status=ready`，scheduler仍維持`desired=stopped`／`observed=stopped`，不得因修復設定而
啟動provider。

## Initial named Admin Owner evidence（2026-09-18）

Production首次具名管理者以break-glass bootstrap建立為`geai_admin`，顯示名稱`GEAI Admin`，角色為
`owner`且帳號啟用。Bootstrap完成後立即以password reset流程標記
`must_change_password=true`並撤銷bootstrap session；另以一次性密碼完成login、`/auth/me`身分確認與
logout驗收，驗收session亦已撤銷。一次性密碼只交付至operator本機剪貼簿，未寫入Git、SSM command、
部署log或本文件。

首次Shioaji host沒有SQLite state，而既有`--require-stopped`按設計只接受可唯讀檢查的既有state；
因此首次cutover先以exact Shioaji digest離線建立空白schema，未載入secret或啟動provider，再重跑
candidate與activation。版本控制內的release helper現在只在state不存在且stable container不存在時執行
同等的`--initialize-state`；建立後必須通過schema/integrity與`10001:10001:0600`metadata檢查。stable
已存在卻遺失state時仍fail closed，operator必須依backup/recovery程序處理，不得自動重建。
