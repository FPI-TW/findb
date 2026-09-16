data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "Root"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${var.aws_account_id}:root"]
    }
  }
  statement {
    sid       = "AwsServices"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:DescribeKey", "kms:ReEncrypt*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com", "logs.${var.aws_region}.amazonaws.com", "cloudtrail.amazonaws.com", "sns.amazonaws.com"]
    }

  }
}
resource "aws_kms_key" "deploy_bundle" {
  description             = "FinDB production deployment bundles"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json
  tags                    = local.common_tags
  lifecycle {
    prevent_destroy = true
  }
}
resource "aws_kms_alias" "deploy_bundle" {
  name          = "alias/findb-production-deploy-bundle"
  target_key_id = aws_kms_key.deploy_bundle.key_id
}
resource "aws_kms_key" "logs" {
  description             = "FinDB production logs and alerts"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json
  tags                    = local.common_tags
  lifecycle {
    prevent_destroy = true
  }
}
resource "aws_kms_alias" "logs" {
  name          = "alias/findb-production-logs"
  target_key_id = aws_kms_key.logs.key_id
}

resource "aws_s3_bucket" "deploy_bundle" {
  bucket        = "findb-production-deploy-bundle-${var.aws_account_id}"
  force_destroy = false
  tags = merge(local.common_tags, {
    DeploymentUnit = "deployment-bundle"
  })
  lifecycle {
    prevent_destroy = true
  }
}
resource "aws_s3_bucket_public_access_block" "deploy_bundle" {
  bucket                  = aws_s3_bucket.deploy_bundle.id
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
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.deploy_bundle.arn
    }
    bucket_key_enabled = false
  }
}
data "aws_iam_policy_document" "deploy_bundle" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.deploy_bundle.arn, "${aws_s3_bucket.deploy_bundle.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
  statement {
    sid       = "DenyMissingKmsKey"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.deploy_bundle.arn]
    }
  }
  statement {
    sid     = "DenyImmutableReleaseOverwrite"
    effect  = "Deny"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.deploy_bundle.arn}/findb/production/accepted/*",
      "${aws_s3_bucket.deploy_bundle.arn}/fetcher/production/accepted/*",
      "${aws_s3_bucket.deploy_bundle.arn}/findb/production/release-tags/*",
      "${aws_s3_bucket.deploy_bundle.arn}/fetcher/production/release-tags/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:if-none-match"
      values   = ["*"]
    }
  }
  statement {
    sid       = "DenyUnencryptedUpload"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}
resource "aws_s3_bucket_policy" "deploy_bundle" {
  bucket = aws_s3_bucket.deploy_bundle.id
  policy = data.aws_iam_policy_document.deploy_bundle.json
}
resource "aws_s3_bucket_lifecycle_configuration" "deploy_bundle" {
  bucket = aws_s3_bucket.deploy_bundle.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_ecr_repository" "production" {
  for_each             = local.repositories
  name                 = each.value.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.deploy_bundle.arn
  }
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = merge(local.common_tags, {
    DeploymentUnit = each.value.unit
  })
  lifecycle {
    prevent_destroy = true
  }
}
resource "aws_ecr_lifecycle_policy" "production" {
  for_each   = local.repositories
  repository = aws_ecr_repository.production[each.key].name
  policy = jsonencode({
    rules = [{
      rulePriority = 1, description = "Expire untagged after 14 days", selection = {
        tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 14
        }, action = {
        type = "expire"
      }
    }]
  })
}
