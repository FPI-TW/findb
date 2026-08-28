locals {
  runtime_secret_specs = {
    "findb/database/application" = {
      unit          = "findb"
      relative_name = "database/application"
      consumer      = "application"
      status        = "active"
    }
    "findb/database/migration" = {
      unit          = "findb"
      relative_name = "database/migration"
      consumer      = "migration"
      status        = "active"
    }
    "findb/api/admin-break-glass" = {
      unit          = "findb"
      relative_name = "api/admin-break-glass"
      consumer      = "ingest"
      status        = "active"
    }
    "findb/api/queue-health-admin" = {
      unit          = "findb"
      relative_name = "api/queue-health-admin"
      consumer      = "ingest"
      status        = "active"
    }
    "findb/api/lookup-serve" = {
      unit          = "findb"
      relative_name = "api/lookup-serve"
      consumer      = "nginx"
      status        = "active"
    }
    "findb/api/static-cache-serve" = {
      unit          = "findb"
      relative_name = "api/static-cache-serve"
      consumer      = "serve-ingest"
      status        = "active"
    }
    "findb/rabbitmq/runtime" = {
      unit          = "findb"
      relative_name = "rabbitmq/runtime"
      consumer      = "rabbitmq"
      status        = "active"
    }
    "findb/r2/canonical-publisher" = {
      unit          = "findb"
      relative_name = "r2/canonical-publisher"
      consumer      = "worker"
      status        = "active"
    }
    "findb/r2/canonical-reader" = {
      unit          = "findb"
      relative_name = "r2/canonical-reader"
      consumer      = "serve"
      status        = "active"
    }
    "findb/registry/ghcr-pull" = {
      unit          = "findb"
      relative_name = "registry/ghcr-pull"
      consumer      = "host-registry-retirement"
      status        = "transitional-inactive"
    }
    "fetcher/api/calendar-serve" = {
      unit          = "fetcher"
      relative_name = "api/calendar-serve"
      consumer      = "all-providers"
      status        = "active"
    }
    "fetcher/api/source/twelve-data" = {
      unit          = "fetcher"
      relative_name = "api/source/twelve-data"
      consumer      = "twelve-data"
      status        = "active"
    }
    "fetcher/api/source/finlab" = {
      unit          = "fetcher"
      relative_name = "api/source/finlab"
      consumer      = "finlab"
      status        = "active"
    }
    "fetcher/api/source/shioaji" = {
      unit          = "fetcher"
      relative_name = "api/source/shioaji"
      consumer      = "shioaji"
      status        = "active"
    }
    "fetcher/provider/twelve-data" = {
      unit          = "fetcher"
      relative_name = "provider/twelve-data"
      consumer      = "twelve-data"
      status        = "active"
    }
    "fetcher/provider/finlab" = {
      unit          = "fetcher"
      relative_name = "provider/finlab"
      consumer      = "finlab"
      status        = "active"
    }
    "fetcher/provider/shioaji" = {
      unit          = "fetcher"
      relative_name = "provider/shioaji"
      consumer      = "shioaji"
      status        = "active"
    }
    "fetcher/r2/raw" = {
      unit          = "fetcher"
      relative_name = "r2/raw"
      consumer      = "all-providers"
      status        = "active"
    }
    "fetcher/registry/ghcr-pull" = {
      unit          = "fetcher"
      relative_name = "registry/ghcr-pull"
      consumer      = "host-registry-retirement"
      status        = "transitional-inactive"
    }
  }

  # Transitional GHCR metadata remains protected until its separately authorized
  # retirement. It is deliberately excluded from every runtime read/decrypt
  # grant; only the metadata resource itself remains under Terraform control.
  active_runtime_secret_arn_patterns_by_unit = {
    for unit in keys(local.unit_config) : unit => [
      for _, spec in local.runtime_secret_specs :
      "arn:${data.aws_partition.current.partition}:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.unit_config[spec.unit].secret_path}${spec.relative_name}-*"
      if spec.unit == unit && spec.status == "active"
    ]
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
      values   = local.active_runtime_secret_arn_patterns_by_unit[each.key]
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
  description             = "${each.value.unit} staging ${each.value.status} runtime secret for ${each.value.consumer}"
  kms_key_id              = aws_kms_key.runtime_secrets[each.value.unit].arn
  recovery_window_in_days = 30
  tags = merge(local.common_tags, {
    DeploymentUnit = each.value.unit
    Consumer       = each.value.consumer
    Purpose        = "runtime-secret"
    RuntimeStatus  = each.value.status
  })

  lifecycle {
    prevent_destroy = true
  }
}
