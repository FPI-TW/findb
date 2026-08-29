data "aws_iam_policy_document" "deploy_trust" {
  for_each = local.unit_config

  statement {
    sid     = "GitHubActionsEnvironmentOnly"
    effect  = "Allow"
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
      values   = ["repo:${var.github_repository}:environment:${each.value.environment_name}"]
    }
  }
}

data "aws_iam_policy_document" "instance_trust" {
  for_each = local.unit_config

  statement {
    sid     = "Ec2Only"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ecr_publisher_trust" {
  for_each = local.ecr_publisher_role_config
  statement {
    sid     = "GitHubActionsMainOnly"
    effect  = "Allow"
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
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  for_each = local.unit_config

  name                 = each.value.deploy_role_name
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust[each.key].json
  max_session_duration = 3600
  tags                 = merge(local.common_tags, { DeploymentUnit = each.key })
}

resource "aws_iam_role" "instance" {
  for_each = local.unit_config

  name                 = each.value.instance_role_name
  assume_role_policy   = data.aws_iam_policy_document.instance_trust[each.key].json
  max_session_duration = 3600
  tags                 = merge(local.common_tags, { DeploymentUnit = each.key })
}

resource "aws_iam_role" "ecr_publisher" {
  for_each             = local.ecr_publisher_role_config
  name                 = each.value.role_name
  assume_role_policy   = data.aws_iam_policy_document.ecr_publisher_trust[each.key].json
  max_session_duration = 3600
  tags                 = merge(local.common_tags, { DeploymentUnit = "${each.key}-ecr-publisher" })
}

resource "aws_iam_instance_profile" "instance" {
  for_each = local.unit_config

  name = each.value.instance_profile_name
  role = aws_iam_role.instance[each.key].name
  tags = merge(local.common_tags, { DeploymentUnit = each.key })
}

// The agent transport needs broad resource="*" for the regional SSM message
// endpoints, but it intentionally omits Parameter Store and all secret APIs.
data "aws_iam_policy_document" "ssm_agent" {
  for_each = local.unit_config

  statement {
    sid    = "SsmAgentTransport"
    effect = "Allow"

    actions = [
      "ec2messages:AcknowledgeMessage",
      "ec2messages:DeleteMessage",
      "ec2messages:FailMessage",
      "ec2messages:GetEndpoint",
      "ec2messages:GetMessages",
      "ec2messages:SendReply",
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
      "ssm:UpdateInstanceInformation",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "SsmAgentDocumentsAndInventory"
    effect = "Allow"

    actions = [
      "ssm:DescribeAssociation",
      "ssm:DescribeDocument",
      "ssm:GetDeployablePatchSnapshotForInstance",
      "ssm:GetDocument",
      "ssm:GetManifest",
      "ssm:ListAssociations",
      "ssm:ListInstanceAssociations",
      "ssm:PutComplianceItems",
      "ssm:PutConfigurePackageResult",
      "ssm:PutInventory",
      "ssm:UpdateAssociationStatus",
      "ssm:UpdateInstanceAssociationStatus",
    ]

    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ssm_agent" {
  for_each = local.unit_config

  name   = "${each.key}-staging-ssm-agent"
  role   = aws_iam_role.instance[each.key].id
  policy = data.aws_iam_policy_document.ssm_agent[each.key].json
}

data "aws_iam_policy_document" "deploy_permissions" {
  for_each = local.unit_config

  statement {
    sid    = "DescribeTargetState"
    effect = "Allow"

    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ssm:DescribeInstanceInformation",
    ]

    resources = ["*"]
  }

  # AWS-owned documents have no account component in their ARN. Keep this
  # resource grant separate from the target tag conditions: those conditions
  # apply to the EC2 resource, not the SSM document resource.
  statement {
    sid    = "SendAwsOwnedRunShellScriptDocument"
    effect = "Allow"

    actions = ["ssm:SendCommand"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunShellScript",
    ]
  }

  statement {
    sid       = "SendCommandToOwnTaggedInstance"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = [each.value.instance_arn]

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Project"
      values   = [var.project_tag]
    }

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Environment"
      values   = [var.environment_tag]
    }

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/DeploymentUnit"
      values   = [each.key]
    }
  }

  statement {
    sid    = "ReadOwnPreflightLogEvents"
    effect = "Allow"

    # FilterLogEvents is scoped to the unit's CloudWatch log group. The
    # workflow queries only event IDs, never command stdout/stderr.
    actions = ["logs:FilterLogEvents"]

    resources = ["${aws_cloudwatch_log_group.ssm[each.key].arn}:*"]
  }

  # Phase 1 only needs preflight. These future bundle permissions are limited
  # to the unit prefix and intentionally do not grant secret or RDS access.
  statement {
    sid    = "OwnDeploymentBundleObjects"
    effect = "Allow"

    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetObject",
      "s3:PutObject",
    ]

    resources = ["${aws_s3_bucket.deploy_bundle.arn}/${each.value.bundle_prefix}*"]
  }

  statement {
    sid       = "ListOwnDeploymentBundlePrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.deploy_bundle.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${each.value.bundle_prefix}*"]
    }
  }

  statement {
    sid    = "EncryptOwnDeploymentBundle"
    effect = "Allow"

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]

    resources = [aws_kms_key.deploy_bundle.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy_permissions" {
  for_each = local.unit_config

  name   = "${each.key}-staging-deploy-least-privilege"
  role   = aws_iam_role.deploy[each.key].id
  policy = data.aws_iam_policy_document.deploy_permissions[each.key].json
}

data "aws_iam_policy_document" "instance_permissions" {
  for_each = local.unit_config

  statement {
    sid    = "ReadOwnRuntimeSecrets"
    effect = "Allow"

    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
    ]

    resources = [
      for key, spec in local.runtime_secret_specs : aws_secretsmanager_secret.active_runtime[key].arn
      if spec.unit == each.key && spec.status == "active"
    ]
  }

  statement {
    sid       = "DecryptOwnRuntimeSecrets"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.runtime_secrets[each.key].arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:SecretARN"
      values   = local.active_runtime_secret_arn_patterns_by_unit[each.key]
    }
  }

  statement {
    sid    = "ReadOwnDeploymentBundleObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
    ]

    resources = ["${aws_s3_bucket.deploy_bundle.arn}/${each.value.bundle_prefix}*"]
  }

  statement {
    sid       = "ListOwnDeploymentBundlePrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.deploy_bundle.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${each.value.bundle_prefix}*"]
    }
  }

  statement {
    sid    = "DecryptOwnDeploymentBundle"
    effect = "Allow"

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
      values   = ["${aws_s3_bucket.deploy_bundle.arn}/${each.value.bundle_prefix}*"]
    }
  }

  statement {
    sid    = "WriteOwnSsmLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]

    resources = [
      aws_cloudwatch_log_group.ssm[each.key].arn,
      "${aws_cloudwatch_log_group.ssm[each.key].arn}:*",
    ]
  }

  // DescribeLogGroups does not support resource-level permissions. The agent
  // calls it before publishing to the unit-scoped log group above.
  statement {
    sid       = "DescribeSsmLogGroups"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  # ECR authorization tokens are regional and cannot be restricted to a
  # repository ARN; every pull/inspection action remains unit-scoped below.
  statement {
    sid       = "GetEcrAuthorizationToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PullAndInspectOwnEcrImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for repository_key in local.ecr_repository_keys_by_unit[each.key] : aws_ecr_repository.staging[repository_key].arn]
  }
}

resource "aws_iam_role_policy" "instance_permissions" {
  for_each = local.unit_config

  name   = "${each.key}-staging-instance-scope"
  role   = aws_iam_role.instance[each.key].id
  policy = data.aws_iam_policy_document.instance_permissions[each.key].json
}

data "aws_iam_policy_document" "ecr_publisher_permissions" {
  for_each = local.ecr_publisher_role_config
  statement {
    sid       = "GetEcrAuthorizationToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid    = "PushAndInspectOwnEcrImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
      "ecr:DescribeImages", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
    ]
    resources = [for repository_key in local.ecr_repository_keys_by_unit[each.key] : aws_ecr_repository.staging[repository_key].arn]
  }

  # The FinDB release-manifest gate runs the selected backend digest with
  # `alembic heads` before deployment. Keep layer download permission limited
  # to that one repository; Fetcher publishers never pull image layers.
  dynamic "statement" {
    for_each = each.key == "findb" ? [true] : []
    content {
      sid       = "PullBackendForMigrationInspection"
      effect    = "Allow"
      actions   = ["ecr:GetDownloadUrlForLayer"]
      resources = [aws_ecr_repository.staging["findb_backend"].arn]
    }
  }
}

resource "aws_iam_role_policy" "ecr_publisher_permissions" {
  for_each = local.ecr_publisher_role_config
  name     = "${each.key}-staging-ecr-publisher"
  role     = aws_iam_role.ecr_publisher[each.key].id
  policy   = data.aws_iam_policy_document.ecr_publisher_permissions[each.key].json
}
