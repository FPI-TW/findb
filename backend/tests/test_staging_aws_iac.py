"""Focused structural checks for the staging AWS control plane."""

import json
import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
TOFU_ROOT = REPO_ROOT / "infra" / "tofu"
BOOTSTRAP_ROOT = TOFU_ROOT / "bootstrap"
STAGING_ROOT = TOFU_ROOT / "staging"
RUNTIME_SECRET_ROOT = REPO_ROOT / "infra" / "deploy" / "runtime-secrets"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _statement_by_sid(policy: str, sid: str) -> str:
    match = re.search(
        rf'''(?msx)^[ \t]*statement[ \t]*\{{
        (?:(?!^[ \t]*statement[ \t]*\{{).)*?
        ^[ \t]*sid[ \t]*=[ \t]*"{re.escape(sid)}"[ \t]*$
        .*?(?=^[ \t]*statement[ \t]*\{{|\Z)''',
        policy,
    )
    assert match is not None, f"statement with sid {sid!r} was not found"
    return match.group(0)


def test_provider_lockfiles_pin_the_same_signed_aws_provider() -> None:
    bootstrap_lock = _read(BOOTSTRAP_ROOT / ".terraform.lock.hcl")
    staging_lock = _read(STAGING_ROOT / ".terraform.lock.hcl")

    assert bootstrap_lock == staging_lock
    assert 'provider "registry.opentofu.org/hashicorp/aws"' in bootstrap_lock
    assert 'version     = "6.61.0"' in bootstrap_lock
    assert "hashes = [" in bootstrap_lock


def test_bootstrap_is_private_versioned_kms_encrypted_and_uses_native_lockfile() -> None:
    bootstrap = "\n".join(_read(path) for path in BOOTSTRAP_ROOT.glob("*.tf"))
    bootstrap_backend_template = _read(BOOTSTRAP_ROOT / "backend.s3.tf.example")
    bootstrap_versions = _read(BOOTSTRAP_ROOT / "versions.tf")
    backend = _read(STAGING_ROOT / "backend.tf")
    outputs = _read(BOOTSTRAP_ROOT / "outputs.tf")
    readme = _read(TOFU_ROOT / "README.md")

    assert 'resource "aws_s3_bucket" "state"' in bootstrap
    assert 'resource "aws_s3_bucket_public_access_block" "state"' in bootstrap
    assert 'resource "aws_s3_bucket_versioning" "state"' in bootstrap
    assert 'status = "Enabled"' in bootstrap
    assert 'resource "aws_s3_bucket_server_side_encryption_configuration" "state"' in bootstrap
    assert 'sse_algorithm     = "aws:kms"' in bootstrap
    assert 'resource "aws_kms_key" "state"' in bootstrap
    assert "allowed_account_ids = [var.aws_account_id]" in bootstrap_versions
    assert "infra/tofu/bootstrap/backend.tf" in _read(REPO_ROOT / ".gitignore")
    assert 'backend "s3"' in bootstrap_backend_template
    assert "encrypt      = true" in bootstrap_backend_template
    assert "use_lockfile = true" in backend
    assert "bucket       =" not in backend
    assert "key          =" not in backend
    assert "kms_key_id   =" not in backend
    assert "dynamodb_table" not in backend
    for setting in (
        "bucket=",
        "key=staging/control-plane.tfstate",
        "region=",
        "encrypt=true",
        "kms_key_id=",
        "use_lockfile=true",
    ):
        assert setting in outputs
        assert setting in readme
    assert "init -reconfigure" in readme
    assert "backend.s3.tf.example" in readme
    assert "init -migrate-state" in readme
    assert "staging/bootstrap.tfstate" in readme
    assert "bounded local-state" in readme
    assert "securely delete" in readme
    assert "native" in readme and "S3 lockfile" in readme
    assert "list-open-id-connect-providers" in readme
    assert "tofu -chdir=infra/tofu/staging import" in readme
    gitignore = _read(REPO_ROOT / ".gitignore")
    assert "**/.terraform/" in gitignore
    assert "*.tfstate" in gitignore
    assert "infra/tofu/bootstrap/backend.tf" in gitignore
    assert "!*.tfvars.example" in gitignore


def test_bootstrap_state_policy_requires_the_exact_customer_managed_kms_key() -> None:
    bootstrap = _read(BOOTSTRAP_ROOT / "main.tf")

    assert 'resource "aws_s3_bucket_policy" "state"' in bootstrap
    assert 'sid    = "DenyInsecureTransport"' in bootstrap
    assert 'sid    = "DenyMissingSseKmsKey"' in bootstrap
    assert 'sid    = "DenyIncorrectSseKmsKey"' in bootstrap
    assert 'sid    = "DenyIncorrectEncryptionHeader"' in bootstrap
    assert 'actions   = ["s3:PutObject"]' in bootstrap
    assert 'variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"' in bootstrap
    assert "values   = [aws_kms_key.state.arn]" in bootstrap
    assert 'variable = "s3:x-amz-server-side-encryption"' in bootstrap
    assert 'values   = ["aws:kms"]' in bootstrap
    outputs = _read(BOOTSTRAP_ROOT / "outputs.tf")
    assert "kms_key_id=${aws_kms_key.state.arn}" in outputs


def test_staging_references_existing_ec2_and_only_manages_required_tags() -> None:
    source = "\n".join(_read(path) for path in STAGING_ROOT.glob("*.tf"))

    assert 'data "aws_instance" "findb"' in source
    assert 'data "aws_instance" "fetcher"' in source
    assert 'resource "aws_instance"' not in source
    assert 'resource "aws_ec2_tag" "required"' in source
    for tag in ("Project", "Environment", "DeploymentUnit", "Owner", "BackupOwner"):
        assert re.search(rf"{tag}\s+=", source)
    assert "associate-iam-instance-profile" in _read(STAGING_ROOT / "outputs.tf")


def test_provider_and_instance_tag_contract_are_explicit_and_have_no_filesystem_markers() -> None:
    versions = _read(STAGING_ROOT / "versions.tf")
    variables = _read(STAGING_ROOT / "variables.tf")
    tfvars = _read(STAGING_ROOT / "terraform.tfvars.example")
    source = "\n".join(_read(path) for path in STAGING_ROOT.glob("*.tf"))
    outputs = _read(STAGING_ROOT / "outputs.tf")
    bootstrap = "\n".join(_read(path) for path in BOOTSTRAP_ROOT.glob("*.tf"))

    assert "allowed_account_ids = [var.aws_account_id]" in versions
    assert 'default = "findb"' in variables
    assert 'project_tag    = "findb"' in tfvars
    assert 'Project = "FinDB"' not in bootstrap
    assert "marker_path" not in source
    assert "required_marker_contract" not in outputs


def test_oidc_trust_is_exactly_bound_to_each_github_environment() -> None:
    iam = _read(STAGING_ROOT / "iam.tf")
    main = _read(STAGING_ROOT / "main.tf")
    tfvars = _read(STAGING_ROOT / "terraform.tfvars.example")

    assert 'resource "aws_iam_openid_connect_provider" "github"' in main
    assert 'resource "terraform_data" "oidc_contract_guard"' in main
    assert 'manage_github_oidc_provider && var.github_oidc_provider_arn == ""' in main
    assert 'github_oidc_provider_arn    = ""' in tfvars
    assert "manage_github_oidc_provider = true" in tfvars
    assert 'data "aws_iam_openid_connect_provider" "github_existing"' in _read(
        STAGING_ROOT / "versions.tf"
    )
    assert 'client_id_list  = ["sts.amazonaws.com"]' in main
    assert 'variable = "token.actions.githubusercontent.com:aud"' in iam
    assert 'values   = ["sts.amazonaws.com"]' in iam
    assert 'variable = "token.actions.githubusercontent.com:sub"' in iam
    assert "repo:${var.github_repository}:environment:${each.value.environment_name}" in iam
    assert re.search(r'environment_name\s*=\s*"staging-findb"', main)
    assert re.search(r'environment_name\s*=\s*"staging-fetcher"', main)
    trust = iam.split('data "aws_iam_policy_document" "deploy_trust"', 1)[1].split(
        'data "aws_iam_policy_document" "instance_trust"', 1
    )[0]
    assert "StringLike" not in trust


def test_infra_plan_role_is_pr_only_and_state_scoped() -> None:
    plan = _read(STAGING_ROOT / "infra_plan.tf")
    outputs = _read(STAGING_ROOT / "outputs.tf")
    variables = _read(STAGING_ROOT / "variables.tf")
    tfvars = _read(STAGING_ROOT / "terraform.tfvars.example")
    readme = _read(TOFU_ROOT / "README.md")

    assert 'data "aws_iam_policy_document" "infra_plan_trust"' in plan
    assert 'resource "aws_iam_role" "infra_plan"' in plan
    assert 'resource "aws_iam_role_policy" "infra_plan"' in plan
    assert 'values   = ["sts.amazonaws.com"]' in plan
    assert 'values   = ["repo:${var.github_repository}:pull_request"]' in plan
    trust = plan.split('data "aws_iam_policy_document" "infra_plan_trust"', 1)[1].split(
        'resource "aws_iam_role" "infra_plan"', 1
    )[0]
    assert "environment" not in trust

    permissions = plan.split('data "aws_iam_policy_document" "infra_plan_permissions"', 1)[1].split(
        'resource "aws_iam_role_policy" "infra_plan"', 1
    )[0]
    assert 'sid    = "ReadResourceLessMetadata"' in permissions
    assert (
        'actions = [\n      "ec2:DescribeInstanceAttribute",\n      "ec2:DescribeInstanceCreditSpecifications",\n      "ec2:DescribeInstances",\n      "ec2:DescribeInstanceTypes",\n      "ec2:DescribeTags",\n      "ec2:DescribeVolumes",\n      "ec2:DescribeVpcs",\n      "kms:ListAliases",\n      "logs:DescribeLogGroups",\n      "ssm:DescribeAssociation",\n      "ssm:ListAssociations",\n      "sts:GetCallerIdentity",\n    ]'
        in permissions
    )
    assert 'resources = ["*"]' in permissions
    assert 'sid    = "ReadExactManagedRoles"' in permissions
    assert "[for role in aws_iam_role.deploy : role.arn]" in permissions
    assert "[for role in aws_iam_role.instance : role.arn]" in permissions
    assert "[for role in aws_iam_role.ecr_publisher : role.arn]" in permissions
    assert "[aws_iam_role.infra_plan.arn, local.infra_plan_dlm_role_arn]" in permissions
    assert ":role/findb-staging-dlm-root-volume-backup" in plan
    assert 'sid    = "ReadExactRootVolumeBackupPolicy"' in permissions
    assert "resources = [local.infra_plan_dlm_policy_arn]" in permissions
    assert ":dlm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:policy/*" in plan
    assert '"dlm:GetLifecyclePolicy"' in permissions
    assert '"dlm:ListTagsForResource"' in permissions
    assert 'sid    = "ReadExactInstanceProfiles"' in permissions
    assert (
        "resources = [for profile in aws_iam_instance_profile.instance : profile.arn]"
        in permissions
    )
    assert 'sid    = "ReadExactGithubOidcProvider"' in permissions
    assert "resources = [local.github_oidc_provider_arn]" in permissions
    assert 'sid    = "ReadExactDeployBundleKey"' in permissions
    assert "resources = [aws_kms_key.deploy_bundle.arn]" in permissions
    assert 'sid    = "ReadExactRuntimeSecretKeys"' in permissions
    assert "resources = [for key in aws_kms_key.runtime_secrets : key.arn]" in permissions
    assert 'sid    = "ReadExactSessionDocuments"' in permissions
    assert (
        "resources = [for document in aws_ssm_document.session_manager_preferences : document.arn]"
        in permissions
    )
    assert "ssm:DescribeDocumentPermission" in permissions
    assert 'sid       = "ReadExactSsmLogGroupTags"' in permissions
    assert "resources = [for group in aws_cloudwatch_log_group.ssm : group.arn]" in permissions
    assert 'sid    = "ReadExactSecretMetadata"' in permissions
    assert (
        "resources = [for secret in aws_secretsmanager_secret.active_runtime : secret.arn]"
        in permissions
    )
    assert "secretsmanager:GetResourcePolicy" in permissions
    assert 'sid       = "ReadExactStateObject"' in permissions
    assert 'sid    = "ListExactStatePrefix"' in permissions
    assert 'sid       = "GetExactStateBucketLocation"' in permissions
    assert 'sid       = "ManageExactNativeStateLockfile"' in permissions
    assert 'sid       = "DecryptExactStateObjects"' in permissions
    assert 'sid       = "GenerateDataKeyForExactStateLockfile"' in permissions
    for forbidden in (
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        '"kms:Encrypt"',
        "iam:PassRole",
        "iam:Create",
        "iam:Delete",
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "sts:AssumeRole",
    ):
        assert forbidden not in permissions
    assert 'actions   = ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]' in permissions
    assert "resources = [local.infra_plan_lock_object_arn]" in permissions
    assert 'test     = "StringEquals"' in permissions
    assert 'variable = "s3:prefix"' in permissions
    assert "StringLikeIfExists" not in permissions
    state_location_statement = permissions.split('sid       = "GetExactStateBucketLocation"', 1)[
        1
    ].split('sid       = "ManageExactNativeStateLockfile"', 1)[0]
    assert 'actions   = ["s3:GetBucketLocation"]' in state_location_statement
    assert "resources = [local.infra_plan_state_bucket_arn]" in state_location_statement
    assert "s3:prefix" not in state_location_statement
    state_prefix_statement = permissions.split('sid    = "ListExactStatePrefix"', 1)[1].split(
        'sid       = "GetExactStateBucketLocation"', 1
    )[0]
    assert 'actions   = ["s3:ListBucket"]' in state_prefix_statement
    assert 'values   = [var.state_key, "${var.state_key}.tflock"]' in state_prefix_statement
    assert 'actions   = ["kms:Decrypt"]' in permissions
    assert "resources = [var.state_kms_key_arn]" in permissions
    assert "iam:ListAttachedRolePolicies" in permissions

    deploy_bucket_statement = permissions.split('sid    = "ReadExactDeployBundleBucket"', 1)[
        1
    ].split('sid       = "ReadExactStateObject"', 1)[0]
    assert "resources = [aws_s3_bucket.deploy_bundle.arn]" in deploy_bucket_statement
    for action in (
        "s3:GetBucketAcl",
        "s3:GetBucketCORS",
        "s3:GetBucketLocation",
        "s3:GetBucketLogging",
        "s3:GetBucketNotification",
        "s3:GetBucketObjectLockConfiguration",
        "s3:GetBucketOwnershipControls",
        "s3:GetBucketPolicy",
        "s3:GetBucketPolicyStatus",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketRequestPayment",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetBucketWebsite",
        "s3:GetEncryptionConfiguration",
        "s3:GetLifecycleConfiguration",
        "s3:GetReplicationConfiguration",
        "s3:ListBucket",
        "s3:ListTagsForResource",
    ):
        assert action in deploy_bucket_statement
    assert "s3:GetBucketLifecycleConfiguration" not in permissions

    assert "s3:ListAllMyBuckets" not in permissions

    generate_key_statement = permissions.split(
        'sid       = "GenerateDataKeyForExactStateLockfile"', 1
    )[1]
    assert 'actions   = ["kms:GenerateDataKey"]' in generate_key_statement
    assert "resources = [var.state_kms_key_arn]" in generate_key_statement
    assert 'variable = "kms:ViaService"' in generate_key_statement
    assert 'values   = ["s3.${var.aws_region}.amazonaws.com"]' in generate_key_statement
    assert 'variable = "kms:EncryptionContext:aws:s3:arn"' in generate_key_statement
    assert "values   = [local.infra_plan_state_bucket_arn]" in generate_key_statement
    assert "aws_kms_key.runtime_secrets" not in generate_key_statement
    assert "aws_kms_key.deploy_bundle" not in generate_key_statement

    decrypt_key_statement = permissions.split('sid       = "DecryptExactStateObjects"', 1)[1].split(
        'sid       = "GenerateDataKeyForExactStateLockfile"', 1
    )[0]
    assert 'actions   = ["kms:Decrypt"]' in decrypt_key_statement
    assert 'variable = "kms:EncryptionContext:aws:s3:arn"' in decrypt_key_statement
    assert "values   = [local.infra_plan_state_bucket_arn]" in decrypt_key_statement
    assert "var.state_bucket_name" in plan
    assert "var.state_key" in plan
    assert 'output "infra_plan_role_arn"' in outputs
    assert 'condition     = var.aws_region == "ap-southeast-1"' in variables
    assert 'condition     = var.aws_account_id == "439622209937"' in variables
    assert 'default     = "staging-infra-plan"' in variables
    assert 'condition     = var.github_repository == "FPI-TW/findb"' in variables
    assert (
        "condition     = var.deploy_bundle_bucket_name == "
        '"findb-staging-deploy-bundle-439622209937"'
    ) in variables
    assert re.search(r'github_repository\s*=\s*"FPI-TW/findb"', tfvars)
    assert 'state_bucket_name    = "findb-staging-tofu-state-439622209937"' in tfvars
    assert 'state_key            = "staging/control-plane.tfstate"' in tfvars
    assert 'deploy_bundle_bucket_name = "findb-staging-deploy-bundle-439622209937"' in tfvars
    assert (
        'state_kms_key_arn    = "arn:aws:kms:ap-southeast-1:439622209937:key/'
        '776159fc-3251-4cd0-98b0-24dfa9e9701d"'
    ) in tfvars
    assert "The current remote state owns" in readme
    assert "exceptional, separately authorized operation" in readme
    assert "separately authorized operator" in readme
    assert "pull-request" in readme
    assert "never authorizes an apply" in readme
    assert "OpenTofu 1.12.6" in readme
    assert "deploy_bundle_bucket_name=findb-staging-deploy-bundle-439622209937" in readme


def test_deploy_and_instance_roles_are_separate_and_unit_scoped() -> None:
    iam = _read(STAGING_ROOT / "iam.tf")
    main = _read(STAGING_ROOT / "main.tf")

    assert 'resource "aws_iam_role" "deploy"' in iam
    assert 'resource "aws_iam_role" "instance"' in iam
    assert 'resource "aws_iam_instance_profile" "instance"' in iam
    assert 'resource "aws_iam_role_policy" "ssm_agent"' in iam
    assert 'actions   = ["ssm:SendCommand"]' in iam
    assert '"${aws_s3_bucket.deploy_bundle.arn}/${each.value.bundle_prefix}*"' in iam
    assert 'variable = "s3:prefix"' in iam
    unit_bundle_prefixes = dict(
        re.findall(
            r"""(?msx)^[ \t]*(findb|fetcher)[ \t]*=[ \t]*\{
            (?:(?!^[ \t]*(?:findb|fetcher)[ \t]*=[ \t]*\{).)*?
            ^[ \t]*bundle_prefix[ \t]*=[ \t]*"([^"]+)"[ \t]*$""",
            main,
        )
    )
    assert unit_bundle_prefixes == {"findb": "findb/", "fetcher": "fetcher/"}

    deploy_policy = iam.split('data "aws_iam_policy_document" "deploy_permissions"', 1)[1].split(
        'resource "aws_iam_role_policy" "deploy_permissions"', 1
    )[0]
    assert "for_each = local.unit_config" in deploy_policy
    assert 'sid    = "SendAwsOwnedRunShellScriptDocument"' in deploy_policy
    assert (
        "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunShellScript"
        in deploy_policy
    )
    assert ":*:document/AWS-RunShellScript" not in deploy_policy
    document_statement = deploy_policy.split('sid    = "SendAwsOwnedRunShellScriptDocument"', 1)[
        1
    ].split('sid       = "SendCommandToOwnTaggedInstance"', 1)[0]
    assert "ssm:resourceTag/" not in document_statement
    target_statement = deploy_policy.split('sid       = "SendCommandToOwnTaggedInstance"', 1)[
        1
    ].split('sid       = "ReadOwnSsmCommandInvocation"', 1)[0]
    assert "resources = [each.value.instance_arn]" in target_statement
    for tag in ("Project", "Environment", "DeploymentUnit"):
        assert f'variable = "ssm:resourceTag/{tag}"' in target_statement
    fin_db_status_statement = deploy_policy.split("# RDS health", 1)[1].split(
        "# GetCommandInvocation", 1
    )[0]
    assert 'for_each = each.key == "findb" ? [true] : []' in fin_db_status_statement
    assert '"ssm:GetCommandInvocation"' not in fin_db_status_statement
    assert '"rds:DescribeDBInstances"' in fin_db_status_statement
    assert 'resources = ["*"]' in fin_db_status_statement
    assert '"ssm:GetCommandInvocation"' not in target_statement
    assert '"rds:DescribeDBInstances"' not in target_statement
    assert "secretsmanager:GetSecretValue" not in deploy_policy
    assert "ssm:ListCommandInvocations" not in deploy_policy
    invocation_statement = _statement_by_sid(deploy_policy, "ReadSsmCommandInvocation")
    assert 'actions   = ["ssm:GetCommandInvocation"]' in invocation_statement
    assert 'resources = ["*"]' in invocation_statement
    assert "not resource-scoped" in deploy_policy
    assert "logs:GetLogEvents" not in deploy_policy
    assert "logs:FilterLogEvents" not in deploy_policy
    assert "logs:Unmask" not in deploy_policy
    assert "secretsmanager:GetSecretValue" not in deploy_policy
    assert re.findall(r'"(rds:[^"]+)"', deploy_policy) == ["rds:DescribeDBInstances"]
    assert "r2" not in deploy_policy.lower()

    deploy_bundle_kms_statement = _statement_by_sid(deploy_policy, "EncryptOwnDeploymentBundle")
    assert '"kms:Decrypt"' in deploy_bundle_kms_statement
    assert '"kms:Encrypt"' in deploy_bundle_kms_statement
    assert '"kms:GenerateDataKey"' in deploy_bundle_kms_statement
    assert "resources = [aws_kms_key.deploy_bundle.arn]" in deploy_bundle_kms_statement
    assert 'variable = "kms:ViaService"' in deploy_bundle_kms_statement
    assert 'values   = ["s3.${var.aws_region}.amazonaws.com"]' in deploy_bundle_kms_statement
    assert 'variable = "kms:EncryptionContext:aws:s3:arn"' in deploy_bundle_kms_statement
    assert 'values   = ["${aws_s3_bucket.deploy_bundle.arn}/${each.value.bundle_prefix}*"]' in (
        deploy_bundle_kms_statement
    )

    deploy_bundle_encryption = main.split(
        'resource "aws_s3_bucket_server_side_encryption_configuration" "deploy_bundle"', 1
    )[1].split('data "aws_iam_policy_document" "deploy_bundle_bucket"', 1)[0]
    assert "bucket_key_enabled = false" in deploy_bundle_encryption

    ssm_agent_policy = iam.split('data "aws_iam_policy_document" "ssm_agent"', 1)[1].split(
        'resource "aws_iam_role_policy" "ssm_agent"', 1
    )[0]
    assert "ssm:ListInstanceAssociations" in ssm_agent_policy
    assert "ssm:UpdateInstanceAssociationStatus" in ssm_agent_policy

    instance_policy = iam.split('data "aws_iam_policy_document" "instance_permissions"', 1)[1]
    assert "secretsmanager:GetSecretValue" in instance_policy
    for action in ("ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"):
        assert action not in instance_policy
    assert "logs:CreateLogGroup" in instance_policy
    assert "logs:DescribeLogGroups" in instance_policy
    assert "logs:PutLogEvents" in instance_policy
    log_write_statement = _statement_by_sid(instance_policy, "WriteOwnSsmLogs")
    assert "aws_cloudwatch_log_group.ssm[each.key].arn" in log_write_statement
    assert 'resources = ["*"]' not in log_write_statement
    for policy, sid in (
        (deploy_policy, "PublishOwnDeploymentFailureMetric"),
        (instance_policy, "PublishStagingOperationalMetrics"),
    ):
        metric_statement = _statement_by_sid(policy, sid)
        assert 'actions   = ["cloudwatch:PutMetricData"]' in metric_statement
        assert 'variable = "cloudwatch:namespace"' in metric_statement
        assert 'values   = ["FinDB/Staging"]' in metric_statement
    rds_metric_statement = _statement_by_sid(instance_policy, "ReadFinDBLatestRestorableTime")
    assert 'actions   = ["rds:DescribeDBInstances"]' in rds_metric_statement
    assert 'for_each = each.key == "findb" ? [true] : []' in instance_policy


def test_runtime_secrets_are_metadata_only_kms_isolated_and_exactly_scoped() -> None:
    secrets = _read(STAGING_ROOT / "secrets.tf")
    iam = _read(STAGING_ROOT / "iam.tf")
    outputs = _read(STAGING_ROOT / "outputs.tf")
    all_staging = "\n".join(_read(path) for path in STAGING_ROOT.glob("*.tf"))

    tofu_secret_ids = set(
        re.findall(r'^    "((?:findb|fetcher)/[^\"]+)" = \{', secrets, re.MULTILINE)
    )
    catalog_secret_ids: set[str] = set()
    for unit in ("findb", "fetcher"):
        catalog = json.loads((RUNTIME_SECRET_ROOT / f"{unit}.json").read_text(encoding="utf-8"))
        catalog_secret_ids.update(
            f"{unit}/{secret['name']}"
            for consumer in catalog["consumers"].values()
            for secret in consumer["secrets"]
            if "staging" in secret.get("targets", ["staging", "production"])
        )
    assert len(catalog_secret_ids) == 17
    assert tofu_secret_ids == catalog_secret_ids
    assert len(tofu_secret_ids) == 17
    assert "registry/ghcr-pull" not in secrets
    assert secrets.count('status        = "active"') == 17
    assert secrets.count('resource "aws_kms_key" "runtime_secrets"') == 1
    assert 'name          = "alias/findb-staging-${each.key}-runtime-secrets"' in secrets
    assert 'resource "aws_secretsmanager_secret" "active_runtime"' in secrets
    runtime_secret_resource = secrets.split(
        'resource "aws_secretsmanager_secret" "active_runtime"', 1
    )[1]
    assert "for_each = local.runtime_secret_specs" in runtime_secret_resource
    assert "prevent_destroy = true" in runtime_secret_resource
    assert "recovery_window_in_days = 30" in secrets
    assert secrets.count("prevent_destroy = true") == 2
    assert 'resource "aws_secretsmanager_secret_version"' not in all_staging
    assert "secret_string" not in all_staging
    assert "secret_binary" not in all_staging
    assert 'sid    = "RuntimeRoleDecryptThroughSecretsManager"' in secrets
    assert 'variable = "kms:ViaService"' in secrets
    assert 'values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]' in secrets
    assert 'variable = "kms:EncryptionContext:SecretARN"' in secrets
    assert "active_runtime_secret_arn_patterns_by_unit" in secrets
    deploy_policy = iam.split('data "aws_iam_policy_document" "deploy_permissions"', 1)[1].split(
        'resource "aws_iam_role_policy" "deploy_permissions"', 1
    )[0]
    assert "secretsmanager:GetSecretValue" not in deploy_policy
    assert "DecryptOwnRuntimeSecrets" not in deploy_policy
    instance_policy = iam.split('data "aws_iam_policy_document" "instance_permissions"', 1)[1]
    assert 'sid    = "ReadOwnRuntimeSecrets"' in instance_policy
    assert "aws_secretsmanager_secret.active_runtime[key].arn" in instance_policy
    assert 'if spec.unit == each.key && spec.status == "active"' in instance_policy
    assert 'sid       = "DecryptOwnRuntimeSecrets"' in instance_policy
    assert "resources = [aws_kms_key.runtime_secrets[each.key].arn]" in instance_policy
    assert (
        "values   = local.active_runtime_secret_arn_patterns_by_unit[each.key]" in instance_policy
    )
    kms_policy = secrets.split('data "aws_iam_policy_document" "runtime_secrets_kms"', 1)[1].split(
        'resource "aws_kms_key" "runtime_secrets"', 1
    )[0]
    assert "aws_secretsmanager_secret.active_runtime" not in kms_policy
    assert "values   = local.active_runtime_secret_arn_patterns_by_unit[each.key]" in kms_policy
    assert 'output "runtime_secret_kms_key_arns"' in outputs
    assert 'output "runtime_secret_names"' in outputs

    moves = _read(STAGING_ROOT / "runtime_secret_moves.tf")
    moved_pairs = set(
        re.findall(
            r'from = aws_secretsmanager_secret\.runtime\["([^"]+)"\]\s+'
            r'to   = aws_secretsmanager_secret\.active_runtime\["([^"]+)"\]',
            moves,
        )
    )
    assert moved_pairs == {(secret_id, secret_id) for secret_id in catalog_secret_ids}
    assert len(moved_pairs) == 17
    assert "registry/ghcr-pull" not in moves


def test_logs_session_preferences_and_bundle_bucket_are_unit_specific() -> None:
    main = _read(STAGING_ROOT / "main.tf")
    variables = _read(STAGING_ROOT / "variables.tf")

    assert 'resource "aws_cloudwatch_log_group" "ssm"' in main
    assert "retention_in_days = var.ssm_log_retention_days" in main
    assert "kms_key_id        = aws_kms_key.deploy_bundle.arn" in main
    assert 'resource "aws_ssm_document" "session_manager_preferences"' in main
    assert "cloudWatchLogGroupName" in main
    assert "cloudWatchEncryptionEnabled" in main
    assert "cloudWatchStreamingEnabled" in main
    session_document = main.split('resource "aws_ssm_document" "session_manager_preferences"', 1)[1]
    assert "kmsKeyId" not in session_document
    assert 'resource "aws_s3_bucket" "deploy_bundle"' in main
    assert 'resource "aws_s3_bucket_versioning" "deploy_bundle"' in main
    assert 'resource "aws_s3_bucket_server_side_encryption_configuration" "deploy_bundle"' in main
    assert 'sse_algorithm     = "aws:kms"' in main
    assert 'resource "aws_s3_bucket_public_access_block" "deploy_bundle"' in main
    assert 'resource "aws_s3_bucket_policy" "deploy_bundle"' in main
    assert 'sid    = "DenyMissingSseKmsKey"' in main
    assert 'sid    = "DenyIncorrectSseKmsKey"' in main
    assert 'sid    = "DenyIncorrectEncryptionHeader"' in main
    deploy_bundle_policy = main.split('data "aws_iam_policy_document" "deploy_bundle_bucket"', 1)[
        1
    ].split('resource "aws_s3_bucket_policy" "deploy_bundle"', 1)[0]
    assert 'variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"' in deploy_bundle_policy
    assert "values   = [aws_kms_key.deploy_bundle.arn]" in deploy_bundle_policy
    assert 'variable = "s3:x-amz-server-side-encryption"' in deploy_bundle_policy
    assert 'values   = ["aws:kms"]' in deploy_bundle_policy
    assert re.search(r'default\s+=\s+"/findb/staging"', variables)

    outputs = _read(STAGING_ROOT / "outputs.tf")
    readme = _read(TOFU_ROOT / "README.md")
    assert 'output "session_manager_document_names"' in outputs
    assert "aws ssm start-session" in readme
    assert "--target i-0942016913367a8b2" in readme
    assert "--document-name SSM-SessionManagerRunShell-findb-staging" in readme
    assert "--target i-05f518ef183bc31a9" in readme
    assert "--document-name SSM-SessionManagerRunShell-fetcher-staging" in readme
    assert "ssm:StartSession" in readme


def test_phase6_native_monitoring_is_private_encrypted_and_bounded() -> None:
    monitoring = _read(STAGING_ROOT / "monitoring.tf")
    iam = _read(STAGING_ROOT / "iam.tf")
    variables = _read(STAGING_ROOT / "variables.tf")
    tfvars = _read(STAGING_ROOT / "terraform.tfvars.example")
    outputs = _read(STAGING_ROOT / "outputs.tf")
    plan = _read(STAGING_ROOT / "infra_plan.tf")
    runbook = _read(REPO_ROOT / "docs" / "operations" / "monitoring.md")

    assert 'resource "aws_kms_key" "operational_alerts"' in monitoring
    assert 'resource "aws_sns_topic" "operational_alerts"' in monitoring
    assert 'resource "aws_sns_topic_policy" "operational_alerts"' in monitoring
    assert 'resource "aws_sns_topic_subscription" "operational_alert_email"' in monitoring
    assert 'protocol  = "email"' in monitoring
    assert "endpoint  = var.operational_alert_email" in monitoring
    subscription = monitoring.split(
        'resource "aws_sns_topic_subscription" "operational_alert_email"', 1
    )[1].split('resource "aws_cloudwatch_metric_alarm" "ec2_status_check_failed"', 1)[0]
    assert "ignore_changes = [endpoint]" in subscription
    assert "depends_on = [aws_sns_topic_policy.operational_alerts]" in subscription
    assert "kms_master_key_id = aws_kms_key.operational_alerts.arn" in monitoring
    assert 'sid    = "DenyInsecureTransport"' in monitoring
    assert 'variable = "aws:SecureTransport"' in monitoring
    assert 'sid    = "AccountRootTopicAdministrationAndSubscriptionLifecycle"' in monitoring
    assert 'sid    = "AllowCloudWatchAlarmPublish"' in monitoring
    assert 'sid    = "DenyPublishUnlessCloudWatchService"' in monitoring
    assert 'sid    = "DenyCloudWatchPublishFromUnexpectedAccount"' in monitoring
    assert 'sid    = "DenyCloudWatchPublishFromUnexpectedAlarm"' in monitoring
    assert 'identifiers = ["cloudwatch.amazonaws.com"]' in monitoring
    assert 'sid    = "CloudWatchPublishEncryptedOperationalAlerts"' in monitoring
    assert 'sid    = "SnsEncryptOperationalAlerts"' in monitoring
    assert 'variable = "kms:EncryptionContext:aws:sns:topicArn"' in monitoring
    assert 'variable = "aws:SourceAccount"' in monitoring
    assert 'variable = "aws:SourceArn"' in monitoring
    assert "operational_alarm_names = concat(" in monitoring
    assert "[for unit in sort(keys(local.unit_config))" in monitoring
    assert "[for alarm_key in sort(keys(local.rds_monitoring_alarms))" in monitoring
    assert "operational_alarm_arns = [" in monitoring
    assert '}:alarm:${alarm_name}"' in monitoring
    # KMS and the SNS Allow both require the exact list; the SNS deny guard
    # repeats it to reject a missing or unexpected source alarm ARN.
    assert monitoring.count("values   = local.operational_alarm_arns") == 3
    assert ":alarm:*" not in monitoring
    kms_cloudwatch_publish = monitoring.split(
        'sid    = "CloudWatchPublishEncryptedOperationalAlerts"', 1
    )[1].split('resource "aws_kms_key" "operational_alerts"', 1)[0]
    topic_cloudwatch_publish = monitoring.split('sid    = "AllowCloudWatchAlarmPublish"', 1)[
        1
    ].split('sid    = "DenyPublishUnlessCloudWatchService"', 1)[0]
    for publisher_policy in (kms_cloudwatch_publish, topic_cloudwatch_publish):
        assert 'test     = "StringEquals"' in publisher_policy
        assert 'variable = "aws:SourceAccount"' in publisher_policy
        assert 'test     = "ArnEquals"' in publisher_policy
        assert 'variable = "aws:SourceArn"' in publisher_policy
        assert "values   = local.operational_alarm_arns" in publisher_policy

    topic_policy = monitoring.split('data "aws_iam_policy_document" "operational_alerts_topic"', 1)[
        1
    ].split('resource "aws_sns_topic_policy" "operational_alerts"', 1)[0]
    assert topic_policy.count('effect = "Allow"') == 2
    assert 'actions   = ["sns:*"]' not in topic_policy
    assert "actions   = local.operational_alert_topic_policy_actions" in topic_policy
    for action in (
        "sns:AddPermission",
        "sns:DeleteTopic",
        "sns:GetDataProtectionPolicy",
        "sns:GetTopicAttributes",
        "sns:ListSubscriptionsByTopic",
        "sns:ListTagsForResource",
        "sns:Publish",
        "sns:PutDataProtectionPolicy",
        "sns:RemovePermission",
        "sns:SetTopicAttributes",
        "sns:Subscribe",
    ):
        assert f'"{action}"' in monitoring
    owner_statement = topic_policy.split(
        'sid    = "AccountRootTopicAdministrationAndSubscriptionLifecycle"', 1
    )[1].split('sid    = "AllowCloudWatchAlarmPublish"', 1)[0]
    assert '"sns:Publish"' not in owner_statement
    assert '"sns:*"' not in owner_statement
    for action in (
        "sns:AddPermission",
        "sns:DeleteTopic",
        "sns:GetTopicAttributes",
        "sns:ListSubscriptionsByTopic",
        "sns:RemovePermission",
        "sns:SetTopicAttributes",
        "sns:Subscribe",
    ):
        assert f'"{action}"' in owner_statement

    # The only Allow statement capable of publishing must be CloudWatch with
    # both exact provenance conditions. Explicit Deny statements prevent an
    # identity-based same-account allow from bypassing that resource policy.
    allow_publish_statement = topic_policy.split('sid    = "AllowCloudWatchAlarmPublish"', 1)[
        1
    ].split('sid    = "DenyPublishUnlessCloudWatchService"', 1)[0]
    assert 'identifiers = ["cloudwatch.amazonaws.com"]' in allow_publish_statement
    assert '"sns:Publish"' in allow_publish_statement
    deny_unless_cloudwatch = topic_policy.split('sid    = "DenyPublishUnlessCloudWatchService"', 1)[
        1
    ].split('sid    = "DenyCloudWatchPublishFromUnexpectedAccount"', 1)[0]
    assert 'test     = "StringNotEquals"' in deny_unless_cloudwatch
    assert 'variable = "aws:PrincipalServiceName"' in deny_unless_cloudwatch
    assert 'values   = ["cloudwatch.amazonaws.com"]' in deny_unless_cloudwatch
    deny_unexpected_account = topic_policy.split(
        'sid    = "DenyCloudWatchPublishFromUnexpectedAccount"', 1
    )[1].split('sid    = "DenyCloudWatchPublishFromUnexpectedAlarm"', 1)[0]
    assert 'test     = "StringNotEquals"' in deny_unexpected_account
    assert 'variable = "aws:SourceAccount"' in deny_unexpected_account
    assert "values   = [data.aws_caller_identity.current.account_id]" in deny_unexpected_account
    deny_unexpected_alarm = topic_policy.split(
        'sid    = "DenyCloudWatchPublishFromUnexpectedAlarm"', 1
    )[1].split('sid    = "DenyInsecureTransport"', 1)[0]
    assert 'test     = "ArnNotEquals"' in deny_unexpected_alarm
    assert 'variable = "aws:SourceArn"' in deny_unexpected_alarm
    assert "values   = local.operational_alarm_arns" in deny_unexpected_alarm

    non_cloudwatch_allow = owner_statement
    assert '"sns:Publish"' not in non_cloudwatch_allow
    assert '"sns:*"' not in non_cloudwatch_allow
    assert 'resource "aws_cloudwatch_metric_alarm" "ec2_status_check_failed"' in monitoring
    assert 'resource "aws_cloudwatch_metric_alarm" "rds_native"' in monitoring
    ec2_alarm = monitoring.split(
        'resource "aws_cloudwatch_metric_alarm" "ec2_status_check_failed"', 1
    )[1].split('resource "aws_cloudwatch_metric_alarm" "rds_native"', 1)[0]
    rds_alarm = monitoring.split('resource "aws_cloudwatch_metric_alarm" "rds_native"', 1)[1].split(
        'resource "aws_cloudwatch_metric_alarm" "custom"', 1
    )[0]
    assert "depends_on = [aws_sns_topic_policy.operational_alerts]" in ec2_alarm
    assert "depends_on = [aws_sns_topic_policy.operational_alerts]" in rds_alarm
    assert 'namespace           = "AWS/EC2"' in monitoring
    assert 'metric_name         = "StatusCheckFailed"' in monitoring
    assert "dimensions          = { InstanceId = each.value.instance_id }" in monitoring
    assert 'namespace           = "AWS/RDS"' in monitoring
    assert (
        "dimensions          = { DBInstanceIdentifier = var.findb_rds_instance_identifier }"
        in monitoring
    )
    for metric in ("FreeStorageSpace", "DatabaseConnections", "ReadLatency", "WriteLatency"):
        assert f'metric_name         = "{metric}"' in monitoring
    for setting in (
        r"period\s+=\s+local\.monitoring_alarm_common\.period",
        r"evaluation_periods\s+=\s+local\.monitoring_alarm_common\.evaluation_periods",
        r"datapoints_to_alarm\s+=\s+local\.monitoring_alarm_common\.datapoints_to_alarm",
        r"treat_missing_data\s+=\s+local\.monitoring_alarm_common\.treat_missing_data",
    ):
        assert len(re.findall(setting, monitoring)) == 2
    for setting in (
        r"actions_enabled\s+=\s+true",
        r"alarm_actions\s+=\s+\[aws_sns_topic\.operational_alerts\.arn\]",
        r"ok_actions\s+=\s+\[\]",
        r"insufficient_data_actions\s+=\s+\[\]",
    ):
        assert len(re.findall(setting, monitoring)) == 3

    assert 'resource "aws_cloudwatch_metric_alarm" "custom"' in monitoring
    assert 'namespace           = "FinDB/Staging"' in monitoring
    for metric in (
        "CollectorSuccess",
        "DiskUsedPercent",
        "DockerContainerHealthy",
        "DockerRestartCount",
        "DockerRuntimeSecurityHealthy",
        "DLMPolicyHealthy",
        "InodeUsedPercent",
        "RabbitMQDiskAlarm",
        "RabbitMQMemoryAlarm",
        "RDSBackupLagSeconds",
        "SchedulerHeartbeatAgeSeconds",
        "TLSCertificateDaysRemaining",
        "DeploymentFailure",
    ):
        assert f'metric_name         = "{metric}"' in monitoring
    assert 'resource "aws_ssm_association" "staging_metric_publisher"' in monitoring
    assert 'schedule_expression              = "rate(30 minutes)"' in monitoring
    assert re.search(r'name\s+=\s+"AWS-RunShellScript"', monitoring)
    assert "publish_staging_metrics.py" in monitoring
    assert "findb-staging-metric-publisher.service" in monitoring
    assert "findb-staging-metric-publisher.timer" in monitoring
    assert "OnUnitActiveSec=5min" in monitoring
    assert "--dlm-policy-id ${aws_dlm_lifecycle_policy.root_volume_backup.id}" in monitoring
    assert "--tls-host ${var.findb_dns_check_name}" in monitoring
    assert "systemctl enable --now findb-staging-metric-publisher.timer" in monitoring
    assert 'sid       = "ReadFinDBDlmPolicyHealth"' in iam
    assert 'actions   = ["dlm:GetLifecyclePolicy"]' in iam
    assert "resources = [aws_dlm_lifecycle_policy.root_volume_backup.arn]" in iam

    for name, default in (
        ("operational_alert_email", None),
        ("monitoring_alarm_period_seconds", "300"),
        ("monitoring_alarm_evaluation_periods", "2"),
        ("monitoring_alarm_datapoints_to_alarm", "2"),
        ("monitoring_alarm_treat_missing_data", '"missing"'),
        ("monitoring_ec2_status_check_failed_threshold", "1"),
        ("monitoring_rds_free_storage_space_threshold_bytes", "5368709120"),
        ("monitoring_rds_database_connections_threshold", "80"),
        ("monitoring_rds_latency_threshold_seconds", "0.1"),
        ("monitoring_disk_used_percent_threshold", "85"),
        ("monitoring_inode_used_percent_threshold", "90"),
        ("monitoring_docker_restart_count_threshold", "3"),
        ("monitoring_scheduler_heartbeat_age_threshold_seconds", "180"),
        ("monitoring_rds_backup_lag_threshold_seconds", "1800"),
        ("monitoring_tls_certificate_days_remaining_threshold", "30"),
    ):
        block = variables.split(f'variable "{name}"', 1)[1].split("variable ", 1)[0]
        assert "validation {" in block
        if default is None:
            assert "default" not in block
        else:
            assert f"default     = {default}" in block
    assert 'operational_alert_email = "on-call@example.invalid"' in tfvars
    assert "do not commit a real contact value" in tfvars
    assert 'output "operational_alert_topic_arn"' in outputs

    permissions = plan.split('data "aws_iam_policy_document" "infra_plan_permissions"', 1)[1]
    for sid in (
        "ReadOperationalAlarmMetadata",
        "ReadExactOperationalAlertKey",
        "ReadExactOperationalAlertTopic",
        "ReadExactOperationalAlertSubscription",
        "ReadStagingOperationalAlarmTags",
        "ReadExactMetricPublisherAssociationTags",
    ):
        assert f'"{sid}"' in permissions
    assert "cloudwatch:DescribeAlarms" in permissions
    assert "cloudwatch:ListTagsForResource" in permissions
    assert "sns:GetTopicAttributes" in permissions
    assert "sns:ListSubscriptionsByTopic" in permissions
    assert "sns:GetSubscriptionAttributes" in permissions
    assert "sns:Publish" not in permissions
    assert "cloudwatch:PutMetricAlarm" not in permissions
    assert "cloudwatch:SetAlarmState" not in permissions
    alarm_metadata = permissions.split('sid       = "ReadOperationalAlarmMetadata"', 1)[1].split(
        'sid    = "ReadExactManagedRoles"', 1
    )[0]
    assert "resources = [local.infra_plan_operational_alarm_arn]" in alarm_metadata
    assert 'resources = ["*"]' not in alarm_metadata
    assert "CloudWatch supports resource-scoped DescribeAlarms" in plan
    assert "alarm:findb-staging-*" in plan
    subscription_read = permissions.split('sid     = "ReadExactOperationalAlertSubscription"', 1)[
        1
    ].split('sid     = "ReadExactOperationalAlarmTags"', 1)[0]
    assert 'resources = ["*"]' in subscription_read
    assert "aws_sns_topic_subscription.operational_alert_email.arn" not in subscription_read
    for condition_key in (
        "aws:RequestedRegion",
        "aws:ResourceTag/Project",
        "aws:ResourceTag/Environment",
        "aws:ResourceTag/DeploymentUnit",
    ):
        assert f'variable = "{condition_key}"' in subscription_read
    assert 'values   = ["operational-alerts"]' in subscription_read
    operational_key_read = permissions.split('sid    = "ReadExactOperationalAlertKey"', 1)[1].split(
        'sid    = "ReadExactSessionDocuments"', 1
    )[0]
    assert "kms:GenerateDataKey" not in operational_key_read
    assert "kms:Decrypt" not in operational_key_read

    for phrase in (
        "IaC declarations, not live-apply or delivery evidence",
        "Declaration, applied state, and live synthetic confirmation",
        "placeholder neither exposes nor replaces",
        "Controlled recipient replacement",
        "ignore_changes = [endpoint]",
        "-replace='aws_sns_topic_subscription.operational_alert_email'",
        "PendingConfirmation",
        "aws cloudwatch set-alarm-state",
        "reset the synthetic alarm",
        "recurring backup-chain acceptance",
        "therefore complete",
        "HA and production recovery",
        "must not be inferred from the staging controls",
        "migration or backup chain",
        "RDS restore rehearsal",
        "RabbitMQ rebuild",
        "SSH ingress/recovery-key retirement",
        "AWS charges can arise",
    ):
        assert phrase in runbook


def test_phase6_root_volume_backup_is_current_volume_bounded_and_retained() -> None:
    backup = _read(STAGING_ROOT / "backup.tf")
    outputs = _read(STAGING_ROOT / "outputs.tf")
    readme = _read(TOFU_ROOT / "README.md")

    assert 'resource "aws_ec2_tag" "root_volume_backup_selection"' in backup
    assert "one(data.aws_instance.findb.root_block_device).volume_id" in backup
    assert "one(data.aws_instance.fetcher.root_block_device).volume_id" in backup
    assert 'key         = "FinDBBackupPolicy"' in backup
    assert 'root_volume_backup_selection = "findb-staging-current-root-daily"' in backup
    assert 'resource "aws_iam_role" "dlm_root_volume_backup"' in backup
    assert 'identifiers = ["dlm.amazonaws.com"]' in backup
    assert "AWSDataLifecycleManagerServiceRole" in backup
    assert 'resource "aws_dlm_lifecycle_policy" "root_volume_backup"' in backup
    assert 'state              = "ENABLED"' in backup
    assert 'policy_type    = "EBS_SNAPSHOT_MANAGEMENT"' in backup
    assert 'resource_types = ["VOLUME"]' in backup
    assert "interval      = 24" in backup
    assert 'interval_unit = "HOURS"' in backup
    assert 'times         = ["09:00"]' in backup
    assert "count = 7" in backup
    assert "copy_tags = true" in backup
    assert 'BackupPurpose = "automated-current-root-backup"' in backup
    assert 'Purpose     = "automated-current-root-backup"' not in backup
    assert 'output "root_volume_backup"' in outputs
    for phrase in (
        "root volumes currently attached",
        "daily 09:00 UTC",
        "retains seven recovery points",
        "snapshot storage",
        "does not prove recurring execution",
    ):
        assert phrase in readme


def test_staging_cd_reports_deployment_failures_with_unit_scoped_metrics() -> None:
    runbook = _read(REPO_ROOT / "docs" / "operations" / "monitoring.md")
    for unit, workflow_name, environment in (
        ("findb", "findb-cd.yml", "staging-findb"),
        ("fetcher", "fetcher-cd.yml", "staging-fetcher"),
    ):
        workflow = _read(REPO_ROOT / ".github" / "workflows" / workflow_name)
        report_job = workflow.split("  report-staging-deployment-failure:", 1)[1]
        assert "always() &&" in report_job
        assert "github.ref == 'refs/heads/main'" in report_job
        assert "vars.STAGING_ECR_CUTOVER_ENABLED == 'true'" in report_job
        assert "needs.deploy.result == 'failure'" in report_job
        assert f"environment: {environment}" in report_job
        assert "id-token: write" in report_job
        assert "--namespace FinDB/Staging" in report_job
        assert "--metric-name DeploymentFailure" in report_job
        assert f"--dimensions DeploymentUnit={unit},Resource=github-actions" in report_job
    assert "aws sns publish" not in runbook
    assert "Direct SNS `Publish` is not a" in runbook
    assert "supported synthetic test" in runbook
