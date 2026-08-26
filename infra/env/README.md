# Deployment environment sources

Application-local `.env.example` files remain next to the application they
configure. Remote deployment values are managed here:

```text
infra/env/
|- staging/
|  |- findb/
|  |  |- remote.env.example
|  |  `- .env.remote
|  `- fetcher/
|     |- remote.env.example
|     `- .env.remote
`- production/
   |- findb/
   |  |- remote.env.example
   |  `- .env.remote
   `- fetcher/
      |- remote.env.example
      `- .env.remote
```

`remote.env.example` is committed and documents the deployment contract.
`.env.remote` contains real values, must remain ignored with mode `0600`, and
must never be copied to EC2 or committed.

Phase 1 adds six non-sensitive AWS/SSM variables to the two staging contracts
only. Populate the role, profile, and log-group values from the accepted
OpenTofu staging outputs after review; the region, account, and DNS name must
match the accepted staging OpenTofu variables and target contract:

| Variable | Purpose |
| --- | --- |
| `AWS_REGION` | Region containing the existing staging EC2 targets. |
| `AWS_ACCOUNT_ID` | Expected account guard for OIDC credentials and STS. |
| `AWS_DEPLOY_ROLE_ARN` | Unit-specific, environment-bound GitHub OIDC deploy role. |
| `AWS_INSTANCE_PROFILE_NAME` | Exact profile/role name expected on the existing EC2 target. |
| `AWS_SSM_LOG_GROUP` | Unit-specific CloudWatch log group for bounded SSM preflight output. |
| `AWS_DNS_CHECK_NAME` | DNS name resolved by the bounded host preflight. |

For example, review `deploy_role_arns`, `instance_profile_names`, and
`ssm_log_group_names` from:

```bash
tofu -chdir=infra/tofu/staging output -json deploy_role_arns
tofu -chdir=infra/tofu/staging output -json instance_profile_names
tofu -chdir=infra/tofu/staging output -json ssm_log_group_names
```

The six names are required only by `staging-findb` and `staging-fetcher`.
Production resources and workflows are outside Phase 1 and remain on the
current SSH deployment contract. Keep all existing SSH secrets, including the
host, user, and key values, until the separately approved Phase 6 migration.

Each target and service pair publishes to an isolated GitHub Environment:

| Target | Service | Local source | GitHub Environment |
| --- | --- | --- | --- |
| Staging | FinDB backend and Dashboard | `infra/env/staging/findb/.env.remote` | `staging-findb` |
| Staging | Fetcher | `infra/env/staging/fetcher/.env.remote` | `staging-fetcher` |
| Production | FinDB backend and Dashboard | `infra/env/production/findb/.env.remote` | `production-findb` |
| Production | Fetcher | `infra/env/production/fetcher/.env.remote` | `production-fetcher` |

Never copy a staging `.env.remote` into production. Start from the matching
production `remote.env.example` and provision independent hosts, databases,
broker credentials, API credentials, R2 buckets, and break-glass keys.

Validate a source without changing GitHub:

```bash
uv --directory backend run python ../infra/env/sync_github_environment.py staging findb
uv --directory backend run python ../infra/env/sync_github_environment.py staging fetcher
uv --directory backend run python ../infra/env/sync_github_environment.py production findb
uv --directory backend run python ../infra/env/sync_github_environment.py production fetcher
```

Publish after reviewing the reported key names:

```bash
uv --directory backend run python ../infra/env/sync_github_environment.py staging findb --apply
uv --directory backend run python ../infra/env/sync_github_environment.py staging fetcher --apply
uv --directory backend run python ../infra/env/sync_github_environment.py production findb --apply
uv --directory backend run python ../infra/env/sync_github_environment.py production fetcher --apply
```

The sync command creates or updates the Environment, restricts deployment to
`main`, then publishes Variables and Secrets. It validates every required value
before making remote changes and never prints values. Serve lookup/cache secrets
are required only when `SERVE_REQUIRE_AUTH=true`; when enabled they must be
different DB-backed credentials.

Fetcher Environments set `FETCHER_SCHEDULER_CONTROL_POLL_SECONDS` (default `30`,
valid range `1`–`30`) for the DB control poll/heartbeat cadence. Desired state
is not an Environment variable; it is stored in FinDB DB and changed by an
owner through Dashboard. Fetcher Environments also set `SHIOAJI_SIMULATION=true`;
the reviewed data-only
account and scheduler reject production trading mode in every target.
The same CD workflow preflights each provider image and its own durable SQLite
state before converging that provider's isolated container. Scheduler rows are
created stopped in each environment and enabled only after controlled rollout. Calendar Serve
and all three provider Source credentials must be pairwise distinct. Removing a
local value does not delete an already-published GitHub Environment value:
delete retired remote values explicitly before redeploying.
