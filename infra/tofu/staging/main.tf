locals {
  github_oidc_provider_url = "https://token.actions.githubusercontent.com"

  github_oidc_provider_arn = var.github_oidc_provider_arn != "" ? var.github_oidc_provider_arn : (var.manage_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "")

  unit_config = {
    findb = {
      environment_name      = "staging-findb"
      instance_id           = var.findb_instance_id
      instance_arn          = data.aws_instance.findb.arn
      deploy_role_name      = var.findb_deploy_role_name
      instance_role_name    = var.findb_instance_role_name
      instance_profile_name = var.findb_instance_profile_name
      bundle_prefix         = "findb/"
      secret_path           = "findb/staging/findb/"
      parameter_path        = "/findb/staging/findb/"
      dns_check_name        = var.findb_dns_check_name
      log_group_name        = "${var.ssm_log_group_prefix}/findb/ssm"
      tags = {
        Project        = var.project_tag
        Environment    = var.environment_tag
        DeploymentUnit = "findb"
        Owner          = var.owner_tag
        BackupOwner    = var.backup_owner_tag
      }
    }
    fetcher = {
      environment_name      = "staging-fetcher"
      instance_id           = var.fetcher_instance_id
      instance_arn          = data.aws_instance.fetcher.arn
      deploy_role_name      = var.fetcher_deploy_role_name
      instance_role_name    = var.fetcher_instance_role_name
      instance_profile_name = var.fetcher_instance_profile_name
      bundle_prefix         = "fetcher/"
      secret_path           = "findb/staging/fetcher/"
      parameter_path        = "/findb/staging/fetcher/"
      dns_check_name        = var.fetcher_dns_check_name
      log_group_name        = "${var.ssm_log_group_prefix}/fetcher/ssm"
      tags = {
        Project        = var.project_tag
        Environment    = var.environment_tag
        DeploymentUnit = "fetcher"
        Owner          = var.owner_tag
        BackupOwner    = var.backup_owner_tag
      }
    }
  }

  required_instance_tags = merge([
    for unit, config in local.unit_config : {
      for key, value in config.tags : "${unit}:${key}" => {
        instance_id = config.instance_id
        key         = key
        value       = value
      }
    }
  ]...)

  common_tags = merge(
    var.tags,
    {
      Project        = var.project_tag
      Environment    = var.environment_tag
      DeploymentUnit = "control-plane"
      Owner          = var.owner_tag
      BackupOwner    = var.backup_owner_tag
    },
  )

}

resource "terraform_data" "account_guard" {
  input = data.aws_caller_identity.current.account_id

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.aws_account_id
      error_message = "The configured AWS credentials are not for the expected staging account."
    }
  }
}

resource "terraform_data" "oidc_contract_guard" {
  input = local.github_oidc_provider_arn

  lifecycle {
    precondition {
      condition     = var.github_oidc_provider_arn != "" || var.manage_github_oidc_provider
      error_message = "Set github_oidc_provider_arn or explicitly enable manage_github_oidc_provider after inventory."
    }

    precondition {
      condition = var.github_oidc_provider_arn == "" || (
        data.aws_iam_openid_connect_provider.github_existing[0].url == local.github_oidc_provider_url
        && contains(data.aws_iam_openid_connect_provider.github_existing[0].client_id_list, "sts.amazonaws.com")
      )
      error_message = "The referenced GitHub OIDC provider must use the GitHub URL and sts.amazonaws.com audience."
    }
  }
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.manage_github_oidc_provider && var.github_oidc_provider_arn == "" ? 1 : 0

  url             = local.github_oidc_provider_url
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [var.github_oidc_thumbprint]

  tags = merge(local.common_tags, { DeploymentUnit = "github-oidc" })

  lifecycle {
    prevent_destroy = true
  }
}

data "aws_iam_policy_document" "deploy_bundle_kms" {
  statement {
    sid    = "AccountRootAdministration"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "S3ServiceEncryption"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:GenerateDataKey*",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.aws_region}.amazonaws.com"]
    }
  }

  statement {
    sid    = "CloudWatchLogsEncryption"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${var.aws_region}.amazonaws.com"]
    }

    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
      "kms:ReEncrypt*",
    ]

    resources = ["*"]

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:${var.ssm_log_group_prefix}/*"]
    }
  }
}

resource "aws_kms_key" "deploy_bundle" {
  description             = "FinDB staging deployment bundle and SSM logging encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.deploy_bundle_kms.json
  tags                    = merge(local.common_tags, { DeploymentUnit = "control-plane" })
}

resource "aws_kms_alias" "deploy_bundle" {
  name          = var.deploy_bundle_kms_alias
  target_key_id = aws_kms_key.deploy_bundle.key_id
}

resource "aws_s3_bucket" "deploy_bundle" {
  bucket        = var.deploy_bundle_bucket_name
  force_destroy = false
  tags          = merge(local.common_tags, { DeploymentUnit = "deployment-bundle" })
}

resource "aws_s3_bucket_public_access_block" "deploy_bundle" {
  bucket = aws_s3_bucket.deploy_bundle.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "deploy_bundle" {
  bucket = aws_s3_bucket.deploy_bundle.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "deploy_bundle" {
  bucket = aws_s3_bucket.deploy_bundle.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deploy_bundle" {
  bucket = aws_s3_bucket.deploy_bundle.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.deploy_bundle.arn
      sse_algorithm     = "aws:kms"
    }

    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "deploy_bundle_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.deploy_bundle.arn,
      "${aws_s3_bucket.deploy_bundle.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyMissingSseKmsKey"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/*"]

    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = ["true"]
    }
  }

  statement {
    sid    = "DenyIncorrectSseKmsKey"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.deploy_bundle.arn]
    }
  }

  statement {
    sid    = "DenyIncorrectEncryptionHeader"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}

resource "aws_s3_bucket_policy" "deploy_bundle" {
  bucket = aws_s3_bucket.deploy_bundle.id
  policy = data.aws_iam_policy_document.deploy_bundle_bucket.json
}

resource "aws_s3_bucket_lifecycle_configuration" "deploy_bundle" {
  bucket = aws_s3_bucket.deploy_bundle.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_ec2_tag" "required" {
  for_each = local.required_instance_tags

  resource_id = each.value.instance_id
  key         = each.value.key
  value       = each.value.value
}

resource "aws_cloudwatch_log_group" "ssm" {
  for_each = local.unit_config

  name              = each.value.log_group_name
  retention_in_days = var.ssm_log_retention_days
  kms_key_id        = aws_kms_key.deploy_bundle.arn
  tags              = merge(local.common_tags, { DeploymentUnit = each.key })
}

resource "aws_ssm_document" "session_manager_preferences" {
  for_each = local.unit_config

  name            = "SSM-SessionManagerRunShell-${each.key}-staging"
  document_type   = "Session"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "1.0"
    description   = "${each.key} staging Session Manager logging preferences"
    sessionType   = "Standard_Stream"
    inputs = {
      cloudWatchLogGroupName      = each.value.log_group_name
      cloudWatchEncryptionEnabled = true
      cloudWatchStreamingEnabled  = true
      s3BucketName                = ""
      s3KeyPrefix                 = ""
      s3EncryptionEnabled         = false
      runAsEnabled                = false
      idleSessionTimeout          = "20"
      maxSessionDuration          = "60"
    }
  })

  tags = merge(local.common_tags, { DeploymentUnit = each.key })
}
