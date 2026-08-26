"""Focused structural checks for the Phase 1 staging AWS control plane."""

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
TOFU_ROOT = REPO_ROOT / "infra" / "tofu"
BOOTSTRAP_ROOT = TOFU_ROOT / "bootstrap"
STAGING_ROOT = TOFU_ROOT / "staging"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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

    assert 'resource "aws_iam_openid_connect_provider" "github"' in main
    assert 'resource "terraform_data" "oidc_contract_guard"' in main
    assert 'manage_github_oidc_provider && var.github_oidc_provider_arn == ""' in main
    assert "manage_github_oidc_provider  = false" in _read(
        STAGING_ROOT / "terraform.tfvars.example"
    )
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
    assert '"findb/"' in main
    assert '"fetcher/"' in main

    deploy_policy = iam.split('data "aws_iam_policy_document" "deploy_permissions"', 1)[1].split(
        'resource "aws_iam_role_policy" "deploy_permissions"', 1
    )[0]
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
    assert "ssm:GetCommandInvocation" not in deploy_policy
    assert "ssm:ListCommandInvocations" not in deploy_policy
    log_statement = deploy_policy.split('sid    = "ReadOwnPreflightLogEvents"', 1)[1].split(
        'sid    = "OwnDeploymentBundleObjects"', 1
    )[0]
    assert 'actions = ["logs:FilterLogEvents"]' in log_statement
    assert 'resources = ["${aws_cloudwatch_log_group.ssm[each.key].arn}:*"]' in log_statement
    assert "secretsmanager:GetSecretValue" not in deploy_policy
    assert "rds:" not in deploy_policy.lower()
    assert "r2" not in deploy_policy.lower()

    ssm_agent_policy = iam.split('data "aws_iam_policy_document" "ssm_agent"', 1)[1].split(
        'resource "aws_iam_role_policy" "ssm_agent"', 1
    )[0]
    assert "ssm:ListInstanceAssociations" in ssm_agent_policy
    assert "ssm:UpdateInstanceAssociationStatus" in ssm_agent_policy

    instance_policy = iam.split('data "aws_iam_policy_document" "instance_permissions"', 1)[1]
    assert "secretsmanager:GetSecretValue" in instance_policy
    assert "ssm:GetParametersByPath" in instance_policy
    assert "logs:CreateLogGroup" in instance_policy
    assert "logs:DescribeLogGroups" in instance_policy
    assert "logs:PutLogEvents" in instance_policy
    log_write_statement = instance_policy.split('sid    = "WriteOwnSsmLogs"', 1)[1].split(
        'sid       = "DescribeSsmLogGroups"', 1
    )[0]
    assert "aws_cloudwatch_log_group.ssm[each.key].arn" in log_write_statement
    assert 'resources = ["*"]' not in log_write_statement


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
