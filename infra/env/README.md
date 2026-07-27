# Deployment environment sources

Application-local `.env.example` files remain next to the application they
configure. Remote deployment values are managed here:

```text
infra/env/
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

The two deployment sources publish to isolated GitHub Environments:

| Service | Local source | GitHub Environment |
| --- | --- | --- |
| FinDB backend and Dashboard | `infra/env/findb/.env.remote` | `staging-findb` |
| Fetcher | `infra/env/fetcher/.env.remote` | `staging-fetcher` |

Validate a source without changing GitHub:

```bash
uv --directory backend run python ../infra/env/sync_github_environment.py findb
uv --directory backend run python ../infra/env/sync_github_environment.py fetcher
```

Publish after reviewing the reported key names:

```bash
uv --directory backend run python ../infra/env/sync_github_environment.py findb --apply
uv --directory backend run python ../infra/env/sync_github_environment.py fetcher --apply
```

The sync command creates or updates the Environment, restricts deployment to
`main`, then publishes Variables and Secrets. It validates every required value
before making remote changes and never prints values.
