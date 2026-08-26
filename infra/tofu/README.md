# Staging OpenTofu control plane

This directory contains the Phase 1 control-plane foundation and the Phase 2
runtime-secret metadata foundation. It creates IAM, target-specific KMS keys,
Secrets Manager metadata, S3, CloudWatch, and SSM resources and references the
two existing staging EC2 instances. It does not put secret versions or values
in OpenTofu state, and it does not import or recreate EC2, RDS, VPC, subnets,
security groups, Cloudflare R2 buckets, or DNS.

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

Before applying, review `staging/terraform.tfvars.example` and set
`github_oidc_provider_arn` to the existing provider ARN when one exists. The
stack references that provider and never recreates it. Only when an IAM
inventory proves that the provider does not exist may an operator explicitly
set `manage_github_oidc_provider = true` with an empty ARN to create it. This
prevents a second provider from being created under a different owner.

If the provider already exists but ownership is intentionally being transferred
to this state, inventory it first, set `manage_github_oidc_provider = true`,
then import the exact ARN before planning:

```bash
aws iam list-open-id-connect-providers --region ap-southeast-1
tofu -chdir=infra/tofu/staging import \
  'aws_iam_openid_connect_provider.github[0]' \
  arn:aws:iam::439622209937:oidc-provider/token.actions.githubusercontent.com
```

Never apply with an empty provider ARN and the default `false` management flag.

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

Then install/enable the distribution's SSM agent using the existing recovery
path, associate the profile, and wait for the node to report `Online`. This is
an explicit cutover step so the current SSH recovery path remains available.
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

Deploy roles can inspect the target, submit the read-only SSM preflight, read
only bounded marker event IDs from their own CloudWatch log group, and use their
own future deployment-bundle prefix. They cannot
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

Run Command and Session Manager output is sent to the unit log group. The
preflight only emits bounded host facts (account, role/profile identity,
markers, tool versions, capacity, time synchronization, and DNS status); it
never prints environment variables, command output containing secrets, or
runtime credential values.
