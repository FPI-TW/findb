# Deployment control plane

`infra/env/` holds the committed, non-secret control-plane configuration that
GitHub Actions needs to reach each deployment target. GitHub Environments keep
**no variables and no secrets**. They exist only as the OIDC identity bound in
the AWS trust policies (`repo:FPI-TW/findb:environment:<target>-<unit>`) and as
protection rules: deployment branch policy `main` for every Environment, and
required reviewers for `production-findb` and `production-fetcher`. Application
configuration never lives here.

```text
infra/env/
|- control_plane.py          # validate / export
|- staging/
|  |- findb.json
|  `- fetcher.json
`- production/
   |- findb.json
   `- fetcher.json
```

## Contract

Every file has the same shape, and both targets of a unit carry exactly the
same key set; only the values differ.

```json
{"schema_version": 1, "deployment_target": "staging", "unit": "findb", "variables": {"...": "..."}}
```

| Key | Purpose |
| --- | --- |
| `AWS_REGION` | `ap-southeast-1`; the only accepted region. |
| `AWS_ACCOUNT_ID` | Expected account guard for OIDC credentials and STS. |
| `AWS_DEPLOY_ROLE_ARN` | `<unit>-<target>-deploy`; bundles, SSM and ECR copies. Cannot read secret values. |
| `AWS_PROMOTION_ROLE_ARN` | `<unit>-production-promotion`; production only, staging keeps `""`. |
| `AWS_PUBLISHER_ROLE_ARN` | `<unit>-staging-ecr-publisher`; staging only, production keeps `""`. |
| `AWS_INSTANCE_PROFILE_NAME` | `<unit>-<target>-instance`, verified by the host preflight. |
| `AWS_SSM_LOG_GROUP` | `/findb/<target>/<unit>/ssm` for bounded SSM output. |
| `ECR_REGISTRY` | Exact account/region registry accepted by manifests and host helpers. |
| `DEPLOY_BUNDLE_BUCKET` | `findb-<target>-deploy-bundle-<account>`. |
| `DEPLOY_BUNDLE_KMS_KEY_ARN` | SSE-KMS key for bundles, acceptance records and tag bindings. |
| `FINDB_PUBLIC_HOST` | Public hostname used by the DNS preflight and acceptance probes. |
| `FULL_MARKET_ENABLED` | Exact `true`/`false`; a runtime kill switch, equal across both units of a target. |
| `RDS_DB_INSTANCE_IDENTIFIER` | FinDB only; automated-backup/PITR health gate. |
| `FETCHER_FULL_MARKET_STATE_DIR` | Fetcher only; `/var/lib/findb-full-market` (production) or `/var/lib/findb-full-market/staging`. |
| `STAGING_AWS_ACCOUNT_ID`, `STAGING_PROMOTION_READ_ROLE_ARN`, `STAGING_ECR_REGISTRY`, `STAGING_DEPLOY_BUNDLE_BUCKET` | Production only; must equal the staging file's values. Staging keeps `""`. |

`python3 infra/env/control_plane.py validate` (part of `pnpm check:infra`) enforces
the role-name formulas, the empty/non-empty rules per target, the cross-target
equalities, the exact flag values and the state-directory shape. Every value is a
string; `""` means "not applicable in this target".

## How workflows read it

`cd.yml`, both reusable deploy workflows and the FinLab smoke check out
`infra/env/` from `github.workflow_sha` (sparse, read-only), verify the checkout
SHA, then run:

```bash
python3 infra/env/control_plane.py export --target <target> --unit <unit> >> "$GITHUB_ENV"
```

Replay and rollback therefore always use the current control plane, even when the
bundle revision predates it. Workflows reference the values as `${{ env.X }}`;
`${{ vars.* }}` and `${{ secrets.* }}` are not used anywhere in the deployment
path (the only repository variable left is `STAGING_INFRA_PLAN_ROLE_ARN` for the
pull-request plan gate).

## Application configuration

Both targets load the same application keys on the host from Secrets Manager
`findb/<target>/<unit>/runtime/configuration` through the instance role. The key
allowlist is `infra/deploy/runtime-secrets/findb.json` and `fetcher.json`; the
catalogs are identical for staging and production, and a per-target `targets`
filter is rejected by the loader. Representative keys:

- FinDB: `SOURCE_ALLOWLIST_CIDRS`, `PORT=8080`, `SERVE_REQUIRE_AUTH`,
  `HISTORICAL_BACKFILL_WINDOW_DAYS` (`31` in staging, `366` in production),
  `FINDB_TLS_CERT_PATH=/run/findb-runtime-secrets/nginx/server.crt`,
  `FINDB_TLS_PRIVATE_KEY_PATH=/run/findb-runtime-secrets/nginx/server.key`.
- Fetcher: `FETCHER_SOURCE_API_URL`, `FINDB_SERVE_BASE_URL`, R2 account/bucket,
  timeouts and retry limits, `SHIOAJI_SIMULATION=true`.

Validate a host before deploying a catalog change:

```bash
sudo /opt/<unit>/current/infra/deploy/runtime-secrets/load_runtime_secrets.py \
  --catalog <catalog> --region ap-southeast-1 --deployment-target <target> \
  --aws-account-id <account> --consumer <consumer> --output <tmpfs path> --check-only
```

## Changing a value

- Control-plane value: edit the JSON in a pull request; CI validates it; the next
  deployment picks it up.
- Application value: `aws secretsmanager put-secret-value --secret-id findb/<target>/<unit>/runtime/configuration --secret-string file://...` with the full
  document; the loader fails closed on missing required keys or unknown keys.

Local `.env.remote` files left over from the retired GitHub Environment sync are
not read by anything and remain ignored; delete them when convenient.
