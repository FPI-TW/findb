# Production Deployment

> Repo內已將CI/CD拆成四個獨立workflow。GitHub Environments、AWS角色、runtime
> secrets與Fetcher EC2仍須在外部建立；workflow檔存在不代表production資源已完成。

## Workflow 邊界

| Workflow | 責任 | 主要觸發 | Environment / concurrency |
| --- | --- | --- | --- |
| `findb-ci.yml` | Backend、migration、Dashboard與contract acceptance | FinDB或contract相關PR/push、手動 | 不讀production environment |
| `findb-cd.yml` | 建置並部署backend與Dashboard | `main`的FinDB部署檔案變更、手動 | `production-findb` / FinDB專屬group |
| `fetcher-ci.yml` | Fetcher lint、test、contract與container build | Fetcher或contract相關PR/push、手動 | 不讀production environment |
| `fetcher-cd.yml` | 發布Fetcher image並交付獨立目標 | `main`的Fetcher runtime/deploy檔案變更、手動 | `production-fetcher` / Fetcher專屬group |

兩個CI可同時因 `contracts/**` 或contract source變更而執行。Contract-only變更不會
自動部署Fetcher；跨版本更新必須依下方backend-first順序，由
`workflow_dispatch`明確啟動需要的CD。

自動CD只可部署同一commit已通過對應CI的artifact；不得用另一個SHA或僅憑branch最新
狀態取代該gate。手動 `workflow_dispatch`仍須受environment protection約束，操作者
也必須確認指定revision的CI結果。

FinDB deployment unit包含backend與Dashboard。FinDB CD會建置兩個image、render nginx
設定、同步production Compose/infra、執行migration，再啟動與驗證serve、ingest、
dispatcher、worker、RabbitMQ、Dashboard及nginx。

Fetcher CD發布：

```text
ghcr.io/fpi-tw/findb-fetcher:<git-sha>
```

並將該immutable tag交付給獨立Fetcher target。現有Fetcher程式只提供contract
validation、readiness與Source API delivery client，尚無provider adapter、持久排程、
checkpoint或production fetch loop；部署image不得被描述為已啟動持續抓取。

Production migration期間必須停止 `ingest`、`dispatcher`、`worker`與其他DB writers。
Serve若與新schema相容，可以持續提供查詢。

## Deployment isolation

外部設定需建立兩個GitHub Environments：

```text
production-findb
production-fetcher
```

每個deployment job只能引用自己的environment。不要在workflow-level或大型job-level
`env:`注入全部secrets，也不要對reusable workflow使用 `secrets: inherit`；逐一傳入
具名secret。

### `production-findb`

| 類型 | Environment設定名稱 |
| --- | --- |
| Secrets | `FINDB_EC2_HOST`、`FINDB_EC2_USER`、`FINDB_EC2_SSH_KEY` |
| Secrets | `DATABASE_URL`、`CELERY_BROKER_URL`、`RABBITMQ_DEFAULT_USER`、`RABBITMQ_DEFAULT_PASS`、`RABBITMQ_ERLANG_COOKIE` |
| Secrets | `SOURCE_API_KEY`、`SERVE_API_KEYS`、`ADMIN_API_KEY` |
| Secrets | `DASHBOARD_USERNAME`、`DASHBOARD_PASSWORD`、`DASHBOARD_SESSION_SECRET`、`FINDB_STATIC_CACHE_SERVE_API_KEY` |
| Variables | `APP_NAME`、`APP_VERSION`、`DEBUG`、`PORT`、`DATABASE_POOL_SIZE`、`DATABASE_MAX_OVERFLOW` |
| Variables | `API_V1_PREFIX`、`API_KEY_HEADER`、`SOURCE_ALLOWLIST_CIDRS`、`SOURCE_TRUST_PROXY_HEADERS`、`SERVE_REQUIRE_AUTH` |
| Variables | `RATE_LIMIT_REQUESTS`、`RATE_LIMIT_WINDOW`、`RAW_RETENTION_ENABLED`、`RAW_RETENTION_DAYS`、`FINDB_STATIC_CACHE_BASE_URL`、`FINDB_LATEST_PRICE_WORKERS` |

### `production-fetcher`

| 類型 | Environment設定名稱 |
| --- | --- |
| Secrets | `FETCHER_EC2_HOST`、`FETCHER_EC2_USER`、`FETCHER_EC2_SSH_KEY` |
| Secrets | `FETCHER_SOURCE_CLIENT_KEY` |
| Variables | `FETCHER_SOURCE_API_URL` |

`GITHUB_TOKEN`由GitHub針對workflow run提供，不是需搬入environment的自訂secret。
Provider、S3/KMS與未來OIDC/SSM設定尚未被現有Fetcher runtime引用；實作時必須以
Fetcher專屬名稱加入 `production-fetcher`。FinDB TLS private key若未來由workflow
管理，只能加入 `production-findb`，不能共用。

FinDB job不得讀provider credentials、Fetcher Source client key、raw storage或Fetcher
部署credential。Fetcher job不得讀 `DATABASE_URL`、RabbitMQ、Admin、Dashboard、TLS、
FinDB Source shared key或FinDB部署credential。

非敏感值，如API URL、port、image name與retention days，放Environment variables。

GitHub Environment只會限制存放在該environment內的secrets/variables；repository-level
secrets仍可能被repository內其他workflow引用。因此上表的production secrets必須實際
搬入對應environment，確認workflow已切換後再從repository scope移除。建立environment、
設定reviewer/branch policy與搬移secret都是GitHub外部作業，repo檔案不會自動完成。

## GitHub protection

- Production environment只允許protected `main`或release tags。
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
- `FinDBInstanceRole`：只能讀 `/findb/prod/*`。
- `FetcherInstanceRole`：只能讀 `/fetcher/prod/*`。

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
