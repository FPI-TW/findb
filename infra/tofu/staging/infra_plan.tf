locals {
  infra_plan_state_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.state_bucket_name}"
  infra_plan_state_object_arn = "${local.infra_plan_state_bucket_arn}/${var.state_key}"
  infra_plan_lock_object_arn  = "${local.infra_plan_state_object_arn}.tflock"
}

data "aws_iam_policy_document" "infra_plan_trust" {
  statement {
    sid     = "GitHubPullRequestOnly"
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
      values   = ["repo:${var.github_repository}:pull_request"]
    }
  }
}

resource "aws_iam_role" "infra_plan" {
  name                 = var.infra_plan_role_name
  assume_role_policy   = data.aws_iam_policy_document.infra_plan_trust.json
  max_session_duration = 3600
  tags                 = merge(local.common_tags, { DeploymentUnit = "infra-plan" })
}

data "aws_iam_policy_document" "infra_plan_permissions" {
  # Only APIs that AWS does not support with a resource-level grant belong in
  # this statement. Every other read below is tied to an exact object owned by
  # or referenced from this stack. No managed-resource mutation, secret-value
  # read, PassRole, or AssumeRole permission is included.
  statement {
    sid    = "ReadResourceLessMetadata"
    effect = "Allow"

    actions = [
      "ec2:DescribeInstanceAttribute",
      "ec2:DescribeInstanceCreditSpecifications",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeTags",
      "ec2:DescribeVolumes",
      "ec2:DescribeVpcs",
      "kms:ListAliases",
      "logs:DescribeLogGroups",
      "sts:GetCallerIdentity",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "ReadExactDeployRoles"
    effect = "Allow"

    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
    ]

    resources = [for role in aws_iam_role.deploy : role.arn]
  }

  statement {
    sid    = "ReadExactInstanceRoles"
    effect = "Allow"

    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
    ]

    resources = [for role in aws_iam_role.instance : role.arn]
  }

  statement {
    sid       = "ReadExactEcrPublisherRoles"
    effect    = "Allow"
    actions   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:ListRolePolicies", "iam:ListRoleTags"]
    resources = [for role in aws_iam_role.ecr_publisher : role.arn]
  }

  statement {
    sid    = "ReadExactInfraPlanRole"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
    ]
    resources = [aws_iam_role.infra_plan.arn]
  }

  statement {
    sid    = "ReadExactInstanceProfiles"
    effect = "Allow"

    actions = [
      "iam:GetInstanceProfile",
      "iam:ListInstanceProfileTags",
    ]

    resources = [for profile in aws_iam_instance_profile.instance : profile.arn]
  }

  statement {
    sid    = "ReadExactGithubOidcProvider"
    effect = "Allow"

    actions = [
      "iam:GetOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviderTags",
    ]

    resources = [local.github_oidc_provider_arn]
  }

  statement {
    sid    = "ReadExactDeployBundleKey"
    effect = "Allow"

    actions = [
      "kms:DescribeKey",
      "kms:GetKeyPolicy",
      "kms:GetKeyRotationStatus",
      "kms:ListResourceTags",
    ]

    resources = [aws_kms_key.deploy_bundle.arn]
  }

  statement {
    sid    = "ReadExactRuntimeSecretKeys"
    effect = "Allow"

    actions = [
      "kms:DescribeKey",
      "kms:GetKeyPolicy",
      "kms:GetKeyRotationStatus",
      "kms:ListResourceTags",
    ]

    resources = [for key in aws_kms_key.runtime_secrets : key.arn]
  }

  statement {
    sid    = "ReadExactSessionDocuments"
    effect = "Allow"

    actions = [
      "ssm:DescribeDocumentPermission",
      "ssm:DescribeDocument",
      "ssm:GetDocument",
      "ssm:ListTagsForResource",
    ]

    resources = [for document in aws_ssm_document.session_manager_preferences : document.arn]
  }

  statement {
    sid       = "ReadExactSsmLogGroupTags"
    effect    = "Allow"
    actions   = ["logs:ListTagsForResource"]
    resources = [for group in aws_cloudwatch_log_group.ssm : group.arn]
  }

  statement {
    sid    = "ReadExactSecretMetadata"
    effect = "Allow"

    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:ListTagsForResource",
    ]

    resources = [for secret in aws_secretsmanager_secret.runtime : secret.arn]
  }

  statement {
    sid       = "ReadExactEcrRepositories"
    effect    = "Allow"
    actions   = ["ecr:DescribeRepositories", "ecr:GetLifecyclePolicy", "ecr:ListTagsForResource"]
    resources = [for repository in aws_ecr_repository.staging : repository.arn]
  }

  # The aws_s3_bucket resource and its v6 provider refresh path use bucket
  # metadata APIs and HeadBucket/ListObjects permission on this exact bucket.
  # Every bucket-capable read is scoped here.
  statement {
    sid    = "ReadExactDeployBundleBucket"
    effect = "Allow"

    actions = [
      "s3:GetAccelerateConfiguration",
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
    ]

    resources = [aws_s3_bucket.deploy_bundle.arn]
  }

  statement {
    sid       = "ReadExactStateObject"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = [local.infra_plan_state_object_arn]
  }

  statement {
    sid    = "ListExactStatePrefix"
    effect = "Allow"

    actions   = ["s3:ListBucket"]
    resources = [local.infra_plan_state_bucket_arn]

    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = [var.state_key, "${var.state_key}.tflock"]
    }
  }

  statement {
    sid       = "GetExactStateBucketLocation"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation"]
    resources = [local.infra_plan_state_bucket_arn]
  }

  statement {
    sid       = "ManageExactNativeStateLockfile"
    effect    = "Allow"
    actions   = ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
    resources = [local.infra_plan_lock_object_arn]
  }

  statement {
    sid       = "DecryptExactStateObjects"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.state_kms_key_arn]

    # The state bucket uses S3 Bucket Keys, whose KMS encryption context is the
    # bucket ARN rather than an individual object ARN. Exact S3 object grants
    # above remain the object-level boundary for this role.

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:aws:s3:arn"
      values   = [local.infra_plan_state_bucket_arn]
    }
  }

  # Native S3 lockfile PutObject uses the state CMK through S3 Bucket Keys.
  # Keep this exception separate from runtime/deployment KMS permissions and
  # constrain it to the exact state key, S3 service, and state bucket context.
  statement {
    sid       = "GenerateDataKeyForExactStateLockfile"
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey"]
    resources = [var.state_kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:aws:s3:arn"
      values   = [local.infra_plan_state_bucket_arn]
    }
  }
}

resource "aws_iam_role_policy" "infra_plan" {
  name   = "staging-infra-plan-read-only"
  role   = aws_iam_role.infra_plan.id
  policy = data.aws_iam_policy_document.infra_plan_permissions.json
}
