# Deployment

> Repo內已將CI/CD拆成四個獨立workflow。GitHub Environments、AWS角色與runtime
> secrets仍須在外部管理。FinDB與Fetcher目前都只部署staging。

## Workflow 邊界

| Workflow | 責任 | 主要觸發 | Environment / concurrency |
| --- | --- | --- | --- |
| `findb-ci.yml` | Backend、migration、Dashboard與contract acceptance | FinDB或contract相關PR/push、手動 | 不讀部署environment |
| `findb-cd.yml` | 建置並部署backend與Dashboard | `main`的FinDB部署檔案變更、手動 | `staging-findb` / FinDB專屬group |
| `fetcher-ci.yml` | Fetcher lint、test、contract與container build | Fetcher或contract相關PR/push、手動 | 不讀部署environment |
| `fetcher-cd.yml` | 發布Fetcher image並交付獨立staging目標 | `main`的Fetcher runtime/deploy檔案變更、手動 | `staging-fetcher` / Fetcher專屬group |

兩個CI可同時因 `contracts/**` 或contract source變更而執行。Contract-only變更不會
自動部署Fetcher；跨版本更新必須依下方backend-first順序，由
`workflow_dispatch`明確啟動需要的CD。

自動CD只可部署同一commit已通過對應CI的artifact；不得用另一個SHA或僅憑branch最新
狀態取代該gate。手動 `workflow_dispatch`仍須受environment protection約束，操作者
也必須確認指定revision的CI結果。

FinDB deployment unit包含backend與Dashboard。FinDB CD會建置兩個image、render nginx
設定、同步remote Compose/infra、執行migration，再啟動與驗證serve、ingest、
dispatcher、worker、RabbitMQ、Dashboard及nginx。

Fetcher CD發布：

```text
ghcr.io/fpi-tw/findb-fetcher:<git-sha>
```

並將該immutable tag交付給獨立Fetcher target。現有Fetcher程式提供contract
validation、readiness、Source API delivery client、具整體deadline的manual
delivery/wait CLI、versioned小型symbol universe、Twelve Data日線adapter，以及
Fetcher-owned SQLite scheduler、persistent retry、checkpoint與exact-byte raw
Cloudflare R2 persistence。Fetcher CD會用exact SHA image執行無外部呼叫的scheduler
preflight，通過後在獨立target維持單一`findb-fetcher-scheduler --run-forever`
container；它不會建立R2 bucket/API token。Workflow存在不代表已實際部署staging
服務。

Remote migration期間必須停止 `ingest`、`dispatcher`、`worker`與其他DB writers。
Serve若與新schema相容，可以持續提供查詢。

## Deployment isolation

外部設定需建立兩個GitHub Environments：

```text
staging-findb
staging-fetcher
```

每個deployment job只能引用自己的environment。不要在workflow-level或大型job-level
`env:`注入全部secrets，也不要對reusable workflow使用 `secrets: inherit`；逐一傳入
具名secret。

### `staging-findb`

| 類型 | Environment設定名稱 |
| --- | --- |
| Secrets | `FINDB_EC2_HOST`、`FINDB_EC2_USER`、`FINDB_EC2_SSH_KEY` |
| Secrets | `DATABASE_URL`、`CELERY_BROKER_URL`、`RABBITMQ_DEFAULT_USER`、`RABBITMQ_DEFAULT_PASS`、`RABBITMQ_ERLANG_COOKIE` |
| Secrets | `SOURCE_API_KEY`、`SERVE_API_KEYS`、`ADMIN_API_KEY` |
| Secrets | `DASHBOARD_USERNAME`、`DASHBOARD_PASSWORD`、`DASHBOARD_SESSION_SECRET`、`FINDB_STATIC_CACHE_SERVE_API_KEY` |
| Secrets | `CLOUDFLARE_R2_CONFIG_READ_API_TOKEN`（`Workers R2 Storage: Read`） |
| Secrets | `CLOUDFLARE_R2_ACCESS_KEY_ID`、`CLOUDFLARE_R2_SECRET_ACCESS_KEY`（bucket-scoped `Object Read & Write`） |
| Variables | `APP_NAME`、`APP_VERSION`、`DEBUG`、`PORT`、`DATABASE_POOL_SIZE`、`DATABASE_MAX_OVERFLOW` |
| Variables | `API_V1_PREFIX`、`API_KEY_HEADER`、`SOURCE_ALLOWLIST_CIDRS`、`SOURCE_TRUST_PROXY_HEADERS`、`SERVE_REQUIRE_AUTH` |
| Variables | `RATE_LIMIT_REQUESTS`、`RATE_LIMIT_WINDOW`、`RAW_RETENTION_ENABLED`、`RAW_RETENTION_DAYS`、`FINDB_STATIC_CACHE_BASE_URL`、`FINDB_LATEST_PRICE_WORKERS` |
| Variables | `CLOUDFLARE_R2_ACCOUNT_ID`、`CLOUDFLARE_R2_BUCKET` |

### `staging-fetcher`

| 類型 | Environment設定名稱 |
| --- | --- |
| Secrets | `FETCHER_EC2_HOST`、`FETCHER_EC2_USER`、`FETCHER_EC2_SSH_KEY` |
| Variables | `FETCHER_SOURCE_API_URL`、`CLOUDFLARE_R2_ACCOUNT_ID`、`CLOUDFLARE_R2_BUCKET` |
| Variables | `CLOUDFLARE_R2_PREFIX`、`CLOUDFLARE_R2_MAX_OBJECT_BYTES`、`TWELVE_DATA_BASE_URL`、`TWELVE_DATA_TIMEOUT_SECONDS`、`TWELVE_DATA_MAX_RESPONSE_BYTES` |
| Variables | `FETCHER_REQUEST_TIMEOUT_SECONDS`、`FETCHER_MAX_ATTEMPTS`、`FETCHER_MAX_RETRY_AFTER_SECONDS` |
| Secrets | `FETCHER_SOURCE_CLIENT_KEY`、`TWELVE_DATA_API_KEY` |
| Secrets | `CLOUDFLARE_R2_ACCESS_KEY_ID`、`CLOUDFLARE_R2_SECRET_ACCESS_KEY`、選用的`CLOUDFLARE_R2_SESSION_TOKEN` |

`GITHUB_TOKEN`由GitHub針對workflow run提供，只用於拉取GHCR image，絕不傳入runtime
container。現階段Source、Twelve Data與R2 secrets由`staging-fetcher` Environment
逐一傳到遠端程序，再用Docker `--env NAME`注入；這是遷移到instance role加
Secrets Manager/Parameter Store前的明確過渡機制，不可使用
`--env NAME=value`出現在command line。Fetcher的R2 credentials與未來OIDC/SSM
設定必須維持Fetcher專屬，不能複製到FinDB。FinDB TLS private key若未來由workflow管理，只能加入
`staging-findb`，不能共用。

R2 credentials依服務分層：Fetcher只持有指定bucket的`Object Read & Write` S3
credentials；FinDB另外持有`Workers R2 Storage: Read` Bearer token供bucket
configuration稽核，以及獨立的bucket-scoped `Object Read & Write` S3
credentials。`Workers R2 Storage: Edit`不屬於任何application runtime；若需修改
lifecycle或bucket lock，必須使用獨立、短效的管理credential。

FinDB不設定全域R2 prefix；它以完整object reference或bucket inventory辨識物件。
Provider-specific prefix由各Fetcher application管理。目前
`CLOUDFLARE_R2_PREFIX`只屬於Twelve Data Fetcher，未來新增provider時必須使用該
provider自己的prefix或由provider identity確定性產生路徑，不能把單一prefix提升為
Backend全域設定。

Fetcher CD會建立並驗證`/var/lib/findb-fetcher`（numeric owner `10001:10001`、mode
`0700`），以bind mount提供給preflight與scheduler，並維持單一scheduler writer。
SQLite state保存schedule job、retry lease與逐symbol checkpoint；container writable
layer、FinDB RDS與RabbitMQ都不能替代此volume。Rollout在停止stable container前先以
exact SHA image對實際mount執行`--check`；之後停止舊服務、啟動唯一candidate，通過
bounded存活與restart-count檢查後以rename原地promote。失敗會移除candidate並恢復舊
container；首次部署失敗則不留下service。State不隨rollback備份或還原。

每次release在pull新image前先reconcile `stable`、`candidate`與`previous`名稱：先移除
競爭中的candidate，保留stable或在其不存在時恢復previous，最後確認保留的stable確實
可啟動。Reconciliation失敗會在preflight與停止舊服務之前以generic error終止，避免
留下兩個running scheduler。Scheduler preflight亦會驗證SQLite quick-check、完整v1
table/column/index/uniqueness，不接受只偽造`user_version=1`的空或不相容資料庫。

Container停止給予30秒grace period，但scheduler目前不攔截SIGTERM來主動釋放執行中
lease；若在job中途被終止，重新啟動後須等該lease到期才會恢復為retry，這是現有
rollback的已知延遲。

Fetcher raw R2 bucket不得綁定public development URL或custom domain；R2 API token只授權
該bucket的Object Read & Write，並以Fetcher專屬secret注入。Cloudflare R2會自動以
AES-256加密所有object及metadata，因此PutObject不得傳入R2不支援的AWS SSE/KMS headers。
R2 raw data lifecycle為30天，bucket lock為7天；規則由Cloudflare R2管理。
Application只傳credential-free `r2://account-id/bucket/key` reference，不產生
presigned URL，也不把Fetcher的R2 credentials送入FinDB。

FinDB job不得讀provider credentials、Fetcher Source client key、raw storage或Fetcher
部署credential。Fetcher job不得讀 `DATABASE_URL`、RabbitMQ、Admin、Dashboard、TLS、
FinDB Source shared key或FinDB部署credential。

非敏感值，如API URL、port、image name與retention days，放Environment variables。

GitHub Environment只會限制存放在該environment內的secrets/variables；repository-level
secrets仍可能被repository內其他workflow引用。因此上表的deployment secrets必須實際
搬入對應environment，確認workflow已切換後再從repository scope移除。建立environment、
設定reviewer/branch policy與搬移secret都是GitHub外部作業，repo檔案不會自動完成。

## GitHub protection

- `staging-findb`與`staging-fetcher`只允許`main`部署。
- 啟用required reviewer與prevent self-review（依GitHub方案能力）。
- `.github/workflows/**`、`infra/**`、production Compose與contract manifest設定
  CODEOWNERS。
- Branch protection禁止未review直接更新 `main`。
- 第三方Actions固定到完整commit SHA；升級由獨立PR審查。
- 每個服務使用獨立concurrency group且 `cancel-in-progress: false`。

Environment只隔離job。若能任意修改workflow並合入main，仍可能要求另一個environment，
因此branch protection、CODEOWNERS與environment review缺一不可。

## AWS credentials與runtime secrets

目標架構不在GitHub保存長效AWS access key或SSH private key：

```text
GitHub Actions
  -> OIDC short-lived credentials
  -> service-specific AWS deploy role
  -> SSM / deployment service
  -> EC2 instance role
  -> service-specific Secrets Manager path
```

角色：

- `GitHubDeployFinDBRole`：只能部署FinDB資源。
- `GitHubDeployFetcherRole`：只能部署Fetcher資源。
- `FinDBInstanceRole`：只能讀 `/findb/staging/*`。
- `FetcherInstanceRole`：只能讀 `/fetcher/staging/*`。

Application secrets由runtime instance role讀取Secrets Manager/Parameter Store；GitHub
Actions只取得部署權限。需要更強隔離時，兩個secret path使用不同KMS key與key policy。

OIDC trust policy必須限制repository與GitHub Environment subject，不能只信任整個
organization。

## Source client key

Fetcher使用Admin API簽發的DB-backed source client key：

- 每個provider/client一把key。
- 綁定固定 `source_name`、`allowed_datasets`與rate limit。
- FinDB只保存hash；plaintext只在簽發時回傳一次。
- Plaintext存入Fetcher的Secrets Manager path，不放FinDB runtime。
- 切換完成後移除legacy共享 `SOURCE_API_KEY`。

## Network boundaries

- Fetch EC2只透過HTTPS呼叫Source API。
- 優先使用private networking或security-group-to-security-group規則。
- Fetch EC2不得取得RDS與RabbitMQ ingress。
- Source nginx allowlist在Cloudflare後必須先以受信任的Cloudflare CIDR還原
  `CF-Connecting-IP`，再對真實client IP執行allowlist。
- RabbitMQ 5672/15672不對EC2 host或Internet公開。
- TLS private key只存在FinDB主機，不經Fetcher。

## Release順序

Contract變更使用backend-first：

1. Contract PR必須同時通過FinDB CI與Fetcher CI。
2. 以FinDB CD的 `workflow_dispatch`（或符合其FinDB path trigger的main更新）先部署
   同時接受舊版與新版contract的backend。
3. 驗證schema endpoint與shadow delivery。
4. 待Fetcher runtime能力完成後，以Fetcher CD的 `workflow_dispatch`部署切換pin版本；
   contract-only merge不會自動執行此步。
5. 觀察attempt、queue、DQ與canonical結果。
6. 經過保留期後才停止舊版。

環境需記錄：

```text
findb_image_sha
dashboard_image_sha
fetcher_image_sha
accepted_contract_versions
```

## Production go/no-go

- RDS snapshot/PITR可用，production clone已演練migration。
- DB preflight通過且保留足夠connection headroom。
- Provider已暫停，或確認retry會沿用同一idempotency key。
- Queue、worker、disk、DB與delivery monitor均有告警。
- 新image與schema的rollback/forward-fix策略已確認。
- 部署後以固定key做smoke delivery，確認run進入terminal state。

## Rollback

- 發現寫入問題時先暫停provider，停止ingest/dispatcher/worker。
- 不刪raw、job、outbox或RabbitMQ volume來「清除錯誤」。
- 不對production schema做即席downgrade。
- 舊image若Alembic revision不同，不能直接作為rollback image。
- 優先部署包含目前migration chain的forward fix；Serve健康時維持唯讀服務。
