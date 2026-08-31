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
        'actions = [\n      "ec2:DescribeInstanceAttribute",\n      "ec2:DescribeInstanceCreditSpecifications",\n      "ec2:DescribeInstances",\n      "ec2:DescribeInstanceTypes",\n      "ec2:DescribeTags",\n      "ec2:DescribeVolumes",\n      "ec2:DescribeVpcs",\n      "kms:ListAliases",\n      "logs:DescribeLogGroups",\n      "sts:GetCallerIdentity",\n    ]'
        in permissions
    )
    assert 'resources = ["*"]' in permissions
    assert 'sid    = "ReadExactDeployRoles"' in permissions
    assert "resources = [for role in aws_iam_role.deploy : role.arn]" in permissions
    assert 'sid    = "ReadExactInstanceRoles"' in permissions
    assert "resources = [for role in aws_iam_role.instance : role.arn]" in permissions
    assert 'sid    = "ReadExactInfraPlanRole"' in permissions
    assert "resources = [aws_iam_role.infra_plan.arn]" in permissions
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
    ].split('sid    = "ReadOwnPreflightLogEvents"', 1)[0]
    assert "resources = [each.value.instance_arn]" in target_statement
    for tag in ("Project", "Environment", "DeploymentUnit"):
        assert f'variable = "ssm:resourceTag/{tag}"' in target_statement
    fin_db_status_statement = deploy_policy.split("# Phase 4 checks", 1)[1].split(
        "# AWS-owned documents", 1
    )[0]
    assert 'for_each = each.key == "findb" ? [true] : []' in fin_db_status_statement
    assert '"ssm:GetCommandInvocation"' in fin_db_status_statement
    assert '"rds:DescribeDBInstances"' in fin_db_status_statement
    assert 'resources = ["*"]' in fin_db_status_statement
    assert '"ssm:GetCommandInvocation"' not in target_statement
    assert '"rds:DescribeDBInstances"' not in target_statement
    assert "secretsmanager:GetSecretValue" not in deploy_policy
    assert "ssm:ListCommandInvocations" not in deploy_policy
    log_statement = deploy_policy.split('sid    = "ReadOwnPreflightLogEvents"', 1)[1].split(
        'sid    = "OwnDeploymentBundleObjects"', 1
    )[0]
    assert 'actions = ["logs:GetLogEvents"]' in log_statement
    assert (
        '"${trimsuffix(aws_cloudwatch_log_group.ssm[each.key].arn, ":*")}:log-stream:*"'
        in log_statement
    )
    assert "logs:FilterLogEvents" not in log_statement
    assert "logs:Unmask" not in log_statement
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
    log_write_statement = instance_policy.split('sid    = "WriteOwnSsmLogs"', 1)[1].split(
        'sid       = "DescribeSsmLogGroups"', 1
    )[0]
    assert "aws_cloudwatch_log_group.ssm[each.key].arn" in log_write_statement
    assert 'resources = ["*"]' not in log_write_statement


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
