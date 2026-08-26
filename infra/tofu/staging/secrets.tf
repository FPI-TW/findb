locals {
  runtime_secret_specs = {
    "findb/database/application" = {
      unit          = "findb"
      relative_name = "database/application"
      consumer      = "application"
    }
    "findb/database/migration" = {
      unit          = "findb"
      relative_name = "database/migration"
      consumer      = "migration"
    }
    "findb/api/admin-break-glass" = {
      unit          = "findb"
      relative_name = "api/admin-break-glass"
      consumer      = "ingest"
    }
    "findb/api/queue-health-admin" = {
      unit          = "findb"
      relative_name = "api/queue-health-admin"
      consumer      = "ingest"
    }
    "findb/api/lookup-serve" = {
      unit          = "findb"
      relative_name = "api/lookup-serve"
      consumer      = "nginx"
    }
    "findb/api/static-cache-serve" = {
      unit          = "findb"
      relative_name = "api/static-cache-serve"
      consumer      = "serve-ingest"
    }
    "findb/rabbitmq/runtime" = {
      unit          = "findb"
      relative_name = "rabbitmq/runtime"
      consumer      = "rabbitmq"
    }
    "findb/r2/canonical-publisher" = {
      unit          = "findb"
      relative_name = "r2/canonical-publisher"
      consumer      = "worker"
    }
    "findb/r2/canonical-reader" = {
      unit          = "findb"
      relative_name = "r2/canonical-reader"
      consumer      = "serve"
    }
    "findb/registry/ghcr-pull" = {
      unit          = "findb"
      relative_name = "registry/ghcr-pull"
      consumer      = "host-registry"
    }
    "fetcher/api/calendar-serve" = {
      unit          = "fetcher"
      relative_name = "api/calendar-serve"
      consumer      = "all-providers"
    }
    "fetcher/api/source/twelve-data" = {
      unit          = "fetcher"
      relative_name = "api/source/twelve-data"
      consumer      = "twelve-data"
    }
    "fetcher/api/source/finlab" = {
      unit          = "fetcher"
      relative_name = "api/source/finlab"
      consumer      = "finlab"
    }
    "fetcher/api/source/shioaji" = {
      unit          = "fetcher"
      relative_name = "api/source/shioaji"
      consumer      = "shioaji"
    }
    "fetcher/provider/twelve-data" = {
      unit          = "fetcher"
      relative_name = "provider/twelve-data"
      consumer      = "twelve-data"
    }
    "fetcher/provider/finlab" = {
      unit          = "fetcher"
      relative_name = "provider/finlab"
      consumer      = "finlab"
    }
    "fetcher/provider/shioaji" = {
      unit          = "fetcher"
      relative_name = "provider/shioaji"
      consumer      = "shioaji"
    }
    "fetcher/r2/raw" = {
      unit          = "fetcher"
      relative_name = "r2/raw"
      consumer      = "all-providers"
    }
    "fetcher/registry/ghcr-pull" = {
      unit          = "fetcher"
      relative_name = "registry/ghcr-pull"
      consumer      = "host-registry"
    }
  }
}

data "aws_iam_policy_document" "runtime_secrets_kms" {
  for_each = local.unit_config

  statement {
    sid    = "AccountAdministration"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }

  statement {
    sid    = "RuntimeRoleDecryptThroughSecretsManager"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.instance[each.key].arn]
    }

    actions   = ["kms:Decrypt"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:SecretARN"
      values = [
        "arn:${data.aws_partition.current.partition}:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${each.value.secret_path}*",
      ]
    }
  }
}

resource "aws_kms_key" "runtime_secrets" {
  for_each = local.unit_config

  description             = "${each.key} staging runtime secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.runtime_secrets_kms[each.key].json
  tags                    = merge(local.common_tags, { DeploymentUnit = each.key, Purpose = "runtime-secrets" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "runtime_secrets" {
  for_each = local.unit_config

  name          = "alias/findb-staging-${each.key}-runtime-secrets"
  target_key_id = aws_kms_key.runtime_secrets[each.key].key_id
}

resource "aws_secretsmanager_secret" "runtime" {
  for_each = local.runtime_secret_specs

  name                    = "${local.unit_config[each.value.unit].secret_path}${each.value.relative_name}"
  description             = "${each.value.unit} staging runtime secret for ${each.value.consumer}"
  kms_key_id              = aws_kms_key.runtime_secrets[each.value.unit].arn
  recovery_window_in_days = 30
  tags = merge(local.common_tags, {
    DeploymentUnit = each.value.unit
    Consumer       = each.value.consumer
    Purpose        = "runtime-secret"
  })

  lifecycle {
    prevent_destroy = true
  }
}
