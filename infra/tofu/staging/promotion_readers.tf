locals {
  production_promotion_readers = {
    findb = {
      environment = "production-findb", prefix = "findb/accepted/", repositories = local.ecr_repository_keys_by_unit.findb
    }
    fetcher = {
      environment = "production-fetcher", prefix = "fetcher/accepted/", repositories = local.ecr_repository_keys_by_unit.fetcher
    }

  }
}

data "aws_iam_policy_document" "production_promotion_reader_trust" {
  for_each = local.production_promotion_readers
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${each.value.environment}"]
    }

  }
}
resource "aws_iam_role" "production_promotion_reader" {
  for_each           = local.production_promotion_readers
  name               = "${each.key}-staging-promotion-reader"
  assume_role_policy = data.aws_iam_policy_document.production_promotion_reader_trust[each.key].json
  tags = merge(local.common_tags, {
    DeploymentUnit = each.key, Purpose = "production-promotion-source"
  })
}
data "aws_iam_policy_document" "production_promotion_reader" {
  for_each = local.production_promotion_readers
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadOwnAcceptedBundle"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/${each.value.prefix}*"]

  }
  statement {
    sid       = "ListOwnAcceptedPrefix"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.deploy_bundle.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${each.value.prefix}*"]
    }

  }
  statement {
    sid       = "DecryptOwnAcceptedBundle"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.deploy_bundle.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.aws_region}.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:s3:arn"
      values   = ["${aws_s3_bucket.deploy_bundle.arn}/${each.value.prefix}*"]
    }

  }
  statement {
    sid       = "PullOwnStagingDigests"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer"]
    resources = [for key in each.value.repositories : aws_ecr_repository.staging[key].arn]

  }
}
resource "aws_iam_role_policy" "production_promotion_reader" {
  for_each = local.production_promotion_readers
  role     = aws_iam_role.production_promotion_reader[each.key].id
  name     = "read-own-accepted-release"
  policy   = data.aws_iam_policy_document.production_promotion_reader[each.key].json
}

output "production_promotion_reader_role_arns" {
  value = {
    for unit, role in aws_iam_role.production_promotion_reader : unit => role.arn
  }
}
