data "aws_iam_policy_document" "github_trust" {
  for_each = local.units
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
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
resource "aws_iam_role" "deploy" {
  for_each           = local.units
  name               = "${each.value.role_prefix}-production-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_trust[each.key].json
  tags = merge(local.common_tags, {
    DeploymentUnit = each.key
  })
}
resource "aws_iam_role" "promotion" {
  for_each           = local.units
  name               = "${each.value.role_prefix}-production-promotion"
  assume_role_policy = data.aws_iam_policy_document.github_trust[each.key].json
  tags = merge(local.common_tags, {
    DeploymentUnit = each.key
  })
}

data "aws_iam_policy_document" "deploy" {
  for_each = local.units
  statement {
    sid       = "Describe"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation"]
    resources = ["*"]
  }
  # Only the FinDB deploy workflow resolves the production RDS endpoint before
  # sending its SSM command. DescribeDBInstances does not support resource-level
  # permissions, so keep the wildcard action isolated from the Fetcher deployer.
  dynamic "statement" {
    for_each = each.key == "findb" ? [true] : []
    content {
      sid       = "ReadFinDBRdsEndpoint"
      actions   = ["rds:DescribeDBInstances"]
      resources = ["*"]
    }
  }
  statement {
    sid       = "SendDocument"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunShellScript"]
  }
  statement {
    sid       = "SendOwnInstance"
    actions   = ["ssm:SendCommand"]
    resources = [aws_instance.unit[each.key].arn]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Environment"
      values   = ["production"]
    }
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/DeploymentUnit"
      values   = [each.key]
    }

  }
  statement {
    sid       = "ReadWriteOwnBundles"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/${each.key}/production/*"]
  }
  statement {
    sid       = "ListOwnBundles"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.deploy_bundle.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${each.key}/production/*"]
    }
  }
  statement {
    sid       = "BundleKms"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.deploy_bundle.arn]
  }
  statement {
    sid       = "PassOwnInstanceRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.instance[each.key].arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
  statement {
    sid       = "DeploymentFailureMetric"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["FinDB/Production"]
    }
  }
}
resource "aws_iam_role_policy" "deploy" {
  for_each = local.units
  role     = aws_iam_role.deploy[each.key].id
  name     = "production-${each.key}-deploy"
  policy   = data.aws_iam_policy_document.deploy[each.key].json
}

data "aws_iam_policy_document" "promotion" {
  for_each = local.units
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "OwnRepositories"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload", "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:ListImages", "ecr:PutImage", "ecr:UploadLayerPart"]
    resources = [for name in local.repositories_by_unit[each.key] : "arn:${data.aws_partition.current.partition}:ecr:${var.aws_region}:${var.aws_account_id}:repository/${name}"]

  }
  statement {
    sid       = "OwnBundleAndReleaseMetadata"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/${each.key}/production/*"]
  }
  statement {
    sid       = "ListOwnBundlePrefix"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.deploy_bundle.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${each.key}/production/*"]
    }
  }
  statement {
    sid       = "BundleKms"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.deploy_bundle.arn]
  }
}
resource "aws_iam_role_policy" "promotion" {
  for_each = local.units
  role     = aws_iam_role.promotion[each.key].id
  name     = "production-${each.key}-promotion"
  policy   = data.aws_iam_policy_document.promotion[each.key].json
}
