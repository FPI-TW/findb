# Production Deployment

> 現況：FinDB backend與Dashboard部署在同一台EC2；Fetcher尚未納入本repo。
> 核准目標：FinDB與Fetcher同repo、不同image、不同workflow、不同EC2。

## Current FinDB deployment

`.github/workflows/deploy.yml` 在 `main`通過測試後：

1. 建置並推送 backend與Dashboard images。
2. Render nginx Source allowlist、Cloudflare real-IP與Serve key設定。
3. 透過SSH/SCP同步Compose與nginx artifacts。
4. 執行DB preflight，停止所有writer，再套Alembic migration。
5. 啟動RabbitMQ、dispatcher、worker、serve、ingest、Dashboard與nginx。
6. 驗證queue topology、worker heartbeat、app readiness與public routes。

Production migration期間必須停止 `ingest`、`dispatcher`、`worker`與其他DB writers。
Serve若與新schema相容，可以持續提供查詢。

## Target deployment isolation

建立兩個GitHub Environments：

```text
production-findb
production-fetcher
```

並拆成：

```text
.github/workflows/deploy-findb.yml
.github/workflows/deploy-fetcher.yml
```

每個deployment job只能引用自己的environment。不要在workflow-level或大型job-level
`env:`注入全部secrets，也不要對reusable workflow使用 `secrets: inherit`；逐一傳入
具名secret。

| Secret/credential | FinDB | Fetcher |
| --- | --- | --- |
| FinDB deploy role/target | 是 | 否 |
| Fetch deploy role/target | 否 | 是 |
| `DATABASE_URL` | 是 | 否 |
| RabbitMQ credentials | 是 | 否 |
| Admin/Dashboard secrets | 是 | 否 |
| Provider credentials | 否 | 是 |
| Fetcher source client key | 否 | 是 |
| Raw object storage access | 否 | 是 |

非敏感值，如API URL、port、image name與retention days，放Environment variables。

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

1. FinDB先部署同時接受舊版與新版contract。
2. 驗證schema endpoint與shadow delivery。
3. 部署Fetcher切換pin版本。
4. 觀察attempt、queue、DQ與canonical結果。
5. 經過保留期後才停止舊版。

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
