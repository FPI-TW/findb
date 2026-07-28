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
