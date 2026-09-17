# Staging OpenTofu control plane

`production-bootstrap/` creates only the Production remote-state KMS key and
private versioned bucket `findb-production-tofu-state-289112218471` in account
`289112218471`. `production/` is an independent state root at
`production/control-plane.tfstate`; it does not import, move, or share staging
state. Its provider has an account allowlist and an additional runtime account
guard.

Production declares the dedicated `10.20.0.0/16` VPC, public compute/private
RDS subnets in `ap-southeast-1a` and `1c`, two EIP-backed SSM-only Ubuntu hosts,
Single-AZ PostgreSQL 16, immutable unit-scoped ECR, encrypted deployment
bundles, runtime secret metadata, DLM, CloudWatch/SNS, multi-region CloudTrail,
GuardDuty, and unit-scoped GitHub OIDC roles. It intentionally creates no
Secrets Manager versions and contains no Cloudflare credentials. Follow
[`docs/operations/production.md`](../../docs/operations/production.md) for the
review/apply/bootstrap order. `PRODUCTION_DEPLOY_ENABLED` must remain absent.

## Staging control-plane details

This directory contains the Phase 1 control-plane foundation and the Phase 2
runtime-secret metadata foundation. It creates IAM, target-specific KMS keys,
Secrets Manager metadata, S3, CloudWatch, and SSM resources and references the
two existing staging EC2 instances. It does not put secret versions or values
in OpenTofu state, and it does not import or recreate EC2, RDS, VPC, subnets,
security groups, Cloudflare R2 buckets, or DNS.

The Phase 6 monitoring stack declares a private KMS-encrypted SNS
operational-alert topic, one required email subscription, native CloudWatch
alarms, and bounded custom metrics for host capacity, container health/restarts
and runtime security, RabbitMQ alarms, scheduler heartbeat, active-feed
anomalies, TLS expiry, deployment failure, DLM health, and RDS backup lag.
Four bounded governance/security signals additionally cover canonical/raw
lineage, the EOD default partition, credential usage aggregate integrity, and
rejected credential events. Two bounded SSM associations install and run the
dependency-free collector every five minutes using the existing instance
roles; no inbound port or persistent credential is added. It manages only
monitoring resources and associations; it does not manage or import the
referenced EC2/RDS resources. The monitoring baseline, including the four
governance/security signals, and its notification path have completed live
staging acceptance. The operator procedure, current evidence, explicit
thresholds, remaining coverage gaps, and cost/retention caveats are in
[`docs/operations/monitoring.md`](../../docs/operations/monitoring.md).

`staging/backup.tf` selects only the root volumes currently attached to the
two reviewed instances, enables a daily 09:00 UTC EBS Data Lifecycle Manager
schedule, and retains seven recovery points. The unique selection tag moves to
a replacement root volume through OpenTofu instead of continuing to back up a
historical detached volume. DLM snapshots are incremental but snapshot storage
still incurs AWS charges. A policy declaration or a one-time manual snapshot
does not prove recurring execution; acceptance requires a DLM-created recovery
point for each selected current volume and a later second cycle that proves
retention continues.

## State bootstrap

Run the bootstrap stack once from a trusted operator workstation after a
read-only inventory confirms that the proposed bucket and KMS alias are not
owned by another IaC stack. Bootstrap deliberately has a bounded local-state
window because it must create the remote-state bucket before that bucket can
hold state:

```bash
tofu -chdir=infra/tofu/bootstrap init -reconfigure
tofu -chdir=infra/tofu/bootstrap plan -var-file=terraform.tfvars
tofu -chdir=infra/tofu/bootstrap apply -var-file=terraform.tfvars
```

The committed bootstrap configuration intentionally has no backend block, so
the first `init` selects the local backend and the first plan can run before the
state bucket exists. Do not copy the S3 backend template before the first apply.

The bootstrap bucket is private, versioned, blocked from public access, and
encrypted with a customer-managed KMS key. The staging backend uses the native
OpenTofu S3 lockfile (`use_lockfile = true`); no DynamoDB lock table is created.
After the apply succeeds, migrate the bootstrap state to its separate
`staging/bootstrap.tfstate` key before doing other work:

```bash
cp infra/tofu/bootstrap/backend.s3.tf.example \
  infra/tofu/bootstrap/backend.tf
tofu -chdir=infra/tofu/bootstrap init -migrate-state \
  -backend-config="bucket=<state-bucket-name>" \
  -backend-config="key=staging/bootstrap.tfstate" \
  -backend-config="region=ap-southeast-1" \
  -backend-config="encrypt=true" \
  -backend-config="kms_key_id=<state-kms-key-arn>" \
  -backend-config="use_lockfile=true"
tofu -chdir=infra/tofu/bootstrap state list
```

Verify the state is present at that S3 key and the `state list` output is
complete. Only after that verification, securely delete the local bootstrap
`terraform.tfstate` and any local state backup files. The generated
`bootstrap_backend_migrate_command` output contains the same six explicit
backend settings; it assumes the ignored `backend.tf` has already been copied
from `backend.s3.tf.example`.

## Staging foundation

### Pending ECR activation order

`staging/ecr.tf` defines five immutable private staging repositories, two
main-only GitHub OIDC publisher roles, and instance-role pull permissions. It
is configuration only: this repository does not claim the resources have been
applied, does not mutate GitHub variables, and does not authorize deployment.

The activation order is deliberately staged. Keep `STAGING_ECR_CUTOVER_ENABLED`
unset or `false` while a separately authorized protected-`main` fresh
zero-delete plan and foundation apply create the ECR/IAM resources. Complete
foundation-level acceptance before any gated application rollout: verify the
exact repositories and settings, publisher/deploy/instance role boundaries,
and safe authentication/authorization checks, including that each instance
role can obtain an ECR authorization token and pull only its allowed images.
Only then set the repository variable to the exact string `true` and manually
trigger the staging workflows. Those workflows publish immutable commit-SHA
images and perform the host rollout; afterward complete live cutover
acceptance and observation. If cutover acceptance fails, set the gate back to
`false` to freeze and block new staging rollouts; this is not a GHCR fallback.
Every other value fails closed before image publication or host deployment.
Staging uses AWS Secrets Manager plus instance-role ECR login; it must not
receive GitHub or GHCR credentials.
Production has separate target-aware ECR promotion workflows, but its AWS
foundation and live acceptance remain separately authorized work. Staging
credentials and GHCR fallback are not production deployment paths.

The FinDB publisher has one additional repository-scoped read capability:
`ecr:GetDownloadUrlForLayer` for `findb/staging/backend` only. FinDB CD uses
it to run `alembic heads` from the selected immutable backend digest before
emitting a release manifest. Fetcher publishers do not receive layer-download
permission, and no publisher receives cross-unit repository access.

The transitional `registry/ghcr-pull` Secrets Manager metadata resources have
not been retired. The pull-request gate is preflight only: it may prove the
bounded two-delete plan, but it never authorizes or performs an apply. Their
Secrets Manager recovery window is 30 days when retirement is eventually
scheduled.

`staging/runtime_secret_moves.tf` preserves state-address history for all 17
active catalog entries: each legacy `runtime` instance moves to the protected
`active_runtime` instance without recreation. The two GHCR legacy addresses
are intentionally the only unmoved instances and therefore the only planned
deletes. Do not remove or alter those exact mappings.

### Controlled staging ECR rollback

Rollback never restores the transitional GHCR credentials. Keep the cutover
gate exactly `true`, then manually dispatch the affected FinDB or Fetcher CD
workflow on protected `main` with `deployment_target=staging` and `image_tag`
set to a previously accepted, lowercase 40-character commit SHA. The workflow
first verifies the SHA is reachable from that protected `main` and that the
current checkout's `docker-compose.prod.yml`, `infra/deploy/runtime-secrets/`
and `infra/nginx/` deployment-contract scope matches the selected SHA; this is
required because current scripts can deploy older images only while that
contract is unchanged. A difference is rejected and requires a forward
compatibility fix before retrying rollback. FinDB then accepts only its two
fixed staging ECR repositories (backend and dashboard), while Fetcher accepts
only its three fixed staging ECR repositories (Twelve Data, FinLab and
Shioaji), each with exact immutable SHA tags.
When `image_tag` is supplied it runs in reuse-only mode: every required image
must already exist under that exact tag or the workflow fails before host
deployment; it never rebuilds the current SHA, accepts a mutable tag, or pulls
from another registry/repository. The ECR publisher role is main-only, so the
existing tag is restricted to artifacts previously published through this
controlled route. After rollback, record the selected SHA and repeat the
unit's bounded acceptance checks before considering a later forward rollout.

The current remote state owns
`aws_iam_openid_connect_provider.github[0]`. The checked-in
`staging/terraform.tfvars.example` therefore intentionally has
`github_oidc_provider_arn = ""` and
`manage_github_oidc_provider = true`: the resource is resolved from this
state's address and is not recreated. Do not copy those values into a fresh
stack without first establishing ownership and state.

An ownership transfer is an exceptional, separately authorized operation. A
trusted operator must first inventory the provider and its current state owner,
coordinate removal from the old state without deleting the AWS object, set
`manage_github_oidc_provider = true` with an empty ARN, and import the exact
provider ARN before planning:

```bash
aws iam list-open-id-connect-providers --region ap-southeast-1
tofu -chdir=infra/tofu/staging import \
  'aws_iam_openid_connect_provider.github[0]' \
  arn:aws:iam::439622209937:oidc-provider/token.actions.githubusercontent.com
```

Never apply with an empty provider ARN and the default `false` management flag.

## Pull-request plan gate

`.github/workflows/staging-infra-plan.yml` runs only for pull requests to
`main` that change `infra/tofu/**` or the workflow itself. It checks out the
same-repository pull-request head only (external fork PRs are rejected before
checkout or AWS credentials), uses pinned OpenTofu 1.12.6 and AWS actions,
initializes the remote backend with the reviewed bucket, state key, region,
KMS key, encryption, and native S3 lockfile settings, then runs recursive
format, validate, and refresh-enabled plan checks.

The gate uses the reviewed nonsecret backend contract
`bucket=findb-staging-tofu-state-439622209937`,
`key=staging/control-plane.tfstate`, `region=ap-southeast-1`,
`encrypt=true`, `kms_key_id=arn:aws:kms:ap-southeast-1:439622209937:key/776159fc-3251-4cd0-98b0-24dfa9e9701d`,
`use_lockfile=true`, and
`deploy_bundle_bucket_name=findb-staging-deploy-bundle-439622209937`.

The `staging-infra-plan` OIDC role is a distinct identity from both deployment
roles. Its trust is limited to
`repo:FPI-TW/findb:pull_request` with `aud=sts.amazonaws.com`; it has only the
read APIs needed to refresh this stack, including exact grants for every
managed deploy, instance, ECR publisher, production promotion-reader, infra-plan,
and DLM role. It also has exact state-object read/list access, exact state KMS
decrypt, and native lockfile Get/Put/Delete access. Its only KMS GenerateDataKey
exception is the exact state CMK through S3 with the state bucket-key encryption
context. It cannot read runtime secret values or mutate managed AWS resources. The workflow
inspects an ephemeral JSON plan and normally fails closed for every delete
action, including replacement, without uploading a plan artifact. Ordinary
infrastructure pull requests call their local reusable workflow with the
default zero-delete policy. Only the dedicated
`chore/staging-phase2-retirement` route pins the reviewed workflow and enables
the two pre-authorized metadata-only deletes. That exception is a PR preflight
rule, not apply authority: retirement mode may show either the bounded two
deletes before retirement or zero deletes after a separately completed
retirement. Its summary contains only the commit, configuration/lockfile
checksums and bounded counts.

After the role is created, configure its output as the nonsecret repository
variable `STAGING_INFRA_PLAN_ROLE_ARN`. The PR workflow does not read a GitHub
secret or environment secret for this identity.

The first creation of the plan role requires a separately authorized operator
apply because a role cannot bootstrap its own OIDC credentials. A pull-request
plan never authorizes an apply. For the two transitional metadata resources,
the destructive retirement runbook is stricter: after the reviewed retirement
PR is merged, a trusted operator must use a clean checkout whose `HEAD` exactly
matches the merged `origin/main` SHA, live-verify that both exact Secret IDs
have no versions or values, and create a fresh saved plan from that checkout.
The immutable guard must receive that exact saved plan with the two approved
addresses and prove `delete_count=2` with no other delete. Only after an
action-time user confirmation may a separate apply identity apply that exact
saved plan. Then verify both 30-day scheduled deletions, an active catalog of
17, and a fresh protected-`main` zero-delete plan. No PR-local plan or
workflow is an apply authority.

```bash
tofu -chdir=infra/tofu/staging init \
  -backend-config="bucket=<state-bucket-name>" \
  -backend-config="key=staging/control-plane.tfstate" \
  -backend-config="region=ap-southeast-1" \
  -backend-config="encrypt=true" \
  -backend-config="kms_key_id=<state-kms-key-arn>" \
  -backend-config="use_lockfile=true"
tofu -chdir=infra/tofu/staging plan -var-file=terraform.tfvars
tofu -chdir=infra/tofu/staging apply -var-file=terraform.tfvars
```

The bootstrap `backend_init_command` output prints the same explicit
configuration. `staging/backend.tf` is intentionally partial; the globally
unique bucket, state key, region, KMS key, encryption, and native lockfile are
never hardcoded there.

The stack enforces the required `Project=findb`, `Environment`, `DeploymentUnit`,
`Owner`, and `BackupOwner` tags on each existing instance. It creates separate
OIDC deploy roles for `staging-findb` and `staging-fetcher`, separate EC2
instance roles/profiles, unit-specific CloudWatch log groups with retention,
and unit-specific Session Manager preference documents. The deploy bundle
bucket is private, versioned, KMS encrypted, and all role object permissions
are restricted to either `findb/` or `fetcher/`.

The AWS provider cannot safely attach a profile to an already-running EC2
instance without taking ownership of its full `aws_instance` resource. Apply
the generated one-time commands only after reviewing the plan:

```bash
tofu -chdir=infra/tofu/staging output -json associate_instance_profile_commands
```

During the initial Phase 1 cutover, install/enable the distribution's SSM agent
using the then-existing recovery path, associate the profile, and wait for the
node to report `Online`. That explicit cutover step retained SSH until the
separate Phase 6 Session Manager acceptance. Staging TCP/22 ingress, both EC2
key-pair resources, and the matching host `authorized_keys` entries have since
been removed; post-removal unit-specific Session Manager sessions verified the
remaining break-glass path and audit trail.
The preflight workflows do not install packages, create filesystem markers,
mutate containers, or read runtime secrets. Their marker contract is the exact
AWS tag set plus the instance identity returned by IMDS.

For operator recovery, use the custom preference document names emitted by
`session_manager_document_names`:

```bash
aws ssm start-session \
  --region ap-southeast-1 \
  --target i-0942016913367a8b2 \
  --document-name SSM-SessionManagerRunShell-findb-staging
aws ssm start-session \
  --region ap-southeast-1 \
  --target i-05f518ef183bc31a9 \
  --document-name SSM-SessionManagerRunShell-fetcher-staging
```

Replace the example target IDs with the IDs confirmed by the read-only
inventory. The operator identity must separately have `ssm:StartSession` (and
the corresponding session/stream permissions); these recovery permissions are
outside both deploy roles by design.

## IAM boundaries

GitHub trust is exact and environment-bound:

```text
repo:FPI-TW/findb:environment:staging-findb
repo:FPI-TW/findb:environment:staging-fetcher
aud = sts.amazonaws.com
```

Deploy roles can inspect the target, submit the bounded SSM commands, and read
their submitted command invocation status/output through `ssm:GetCommandInvocation`.
That action is not resource-scoped by IAM, so both deploy roles receive it in
an isolated `Resource="*"` statement. Deployment correctness is determined by
the exact inline SSM stdout marker only after an invocation reports `Success`;
CloudWatch remains the durable stderr audit sink and is not read by deploy
roles. Deploy roles can use their own future deployment-bundle prefix. They cannot
call `secretsmanager:GetSecretValue`, read RDS, access another unit's target,
or use another unit's bundle prefix. Instance roles use a scoped SSM agent
transport policy and have exact access to their own declared Secrets Manager
resources, their own runtime-secret KMS key, their own SSM parameter path, and
their deployment-bundle prefix. The agent transport policy itself intentionally
does not grant parameter or secret reads. This does not migrate or delete the
current GitHub Environment runtime secrets.

## Phase 2 runtime-secret metadata and loader

`staging/secrets.tf` declares metadata only for the FinDB and Fetcher runtime
secret catalogs. Each unit has a separate customer-managed KMS key and alias;
the corresponding instance role can decrypt only through Secrets Manager and
only when the encryption context names its own secret prefix. No
`aws_secretsmanager_secret_version`, `secret_string`, provider credential, RDS
URL, API key, or registry token belongs in OpenTofu configuration or state.

The versioned catalogs and host loader live in
`infra/deploy/runtime-secrets/`. The loader accepts only a named consumer from
the catalog, validates the exact JSON keys, rejects credential reuse required
by the catalog, and writes a shell-sourceable `0600` file only at
`/run/findb-runtime-secrets/<consumer>/runtime.env` after verifying the opened
root and consumer directories are both backed by `tmpfs`. Directory-relative, no-follow creation
prevents a symlink or parent-directory race from redirecting the write.
`--check-only` removes the file through the already-open directory immediately
after validation. A deployment caller must remove a normal output file after
sourcing it and unset the loaded shell variables when the operation ends.

The loader deliberately requires the AWS CLI and fails closed with the bounded
reason `aws_cli_missing`; it does not install packages or fall back to a
different credential path. On 2026-08-26 both staging hosts installed AWS CLI
v2.36.31 and verified their unit-specific instance-role identity. The same
acceptance verified the loader's open-file-descriptor check sees `/run` as
`tmpfs` and that the installation staging directory was removed. AWS CLI
installation remains an explicit host prerequisite outside this stack. The
post-install inventory confirmed that no `findb/staging/` secrets and no
runtime-secret KMS aliases existed yet. Review a fresh plan and require zero
destroys before applying this metadata foundation.

After apply, a trusted operator writes each first `AWSCURRENT` version directly
to Secrets Manager without routing values through GitHub Actions, shell command
arguments, OpenTofu variables, or state. Keep existing GitHub Environment
runtime secrets until the `aws-check` canary and consumer-specific deployment
acceptance succeed; rotation and revocation are separate, ordered cutover
actions.

Run Command and Session Manager output is sent to the unit log group. Each
Run Command host script redirects ordinary output to stderr for this durable
audit stream, while fd 3 retains the original stdout solely for the final
success or failure marker. The preflight only emits bounded host facts
(account, role/profile identity, markers, tool versions, capacity, time
synchronization, and DNS status); it never prints environment variables,
command output containing secrets, or runtime credential values.
