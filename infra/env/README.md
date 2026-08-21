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

Validation fails closed when an active `.env.remote` assignment is duplicated,
when the local source contains a name outside the service contract, or when an
existing remote Environment contains an unsupported variable/secret name. The
sync command never chooses between duplicate values and never deletes retired
remote names; the operator must resolve the local duplicate or explicitly remove
the retired remote setting before applying the contract again.

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
