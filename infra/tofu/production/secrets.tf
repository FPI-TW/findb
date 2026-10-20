locals {
  secret_catalog = {
    "fetcher/api/source/factset" = {
      unit = "fetcher", consumer = "factset"
    }
    "fetcher/provider/factset" = {
      unit = "fetcher", consumer = "factset"
    }
    "findb/database/application" = {
      unit = "findb", consumer = "application"
    }
    "findb/database/migration" = {
      unit = "findb", consumer = "migration"
    }
    "findb/api/admin-break-glass" = {
      unit = "findb", consumer = "admin"
    }
    "findb/api/queue-health-admin" = {
      unit = "findb", consumer = "ingest"
    }
    "findb/api/lookup-serve" = {
      unit = "findb", consumer = "nginx"
    }
    "findb/api/static-cache-serve" = {
      unit = "findb", consumer = "serve"
    }
    "findb/rabbitmq/runtime" = {
      unit = "findb", consumer = "rabbitmq"
    }
    "findb/r2/canonical-publisher" = {
      unit = "findb", consumer = "worker"
    }
    "findb/r2/canonical-reader" = {
      unit = "findb", consumer = "serve"
    }
    "findb/runtime/configuration" = {
      unit = "findb", consumer = "compose"
    }
    "findb/tls/origin-certificate" = {
      unit = "findb", consumer = "nginx"
    }
    "fetcher/api/source/twelve-data" = {
      unit = "fetcher", consumer = "twelve-data"
    }
    "fetcher/api/source/finlab" = {
      unit = "fetcher", consumer = "finlab"
    }
    "fetcher/api/source/shioaji" = {
      unit = "fetcher", consumer = "shioaji"
    }
    "fetcher/provider/twelve-data" = {
      unit = "fetcher", consumer = "twelve-data"
    }
    "fetcher/provider/finlab" = {
      unit = "fetcher", consumer = "finlab"
    }
    "fetcher/provider/shioaji" = {
      unit = "fetcher", consumer = "shioaji"
    }
    "fetcher/r2/raw" = {
      unit = "fetcher", consumer = "all-providers"
    }
    "fetcher/runtime/configuration" = {
      unit = "fetcher", consumer = "compose"
    }

    "fetcher/api/source/taifex" = {
      unit = "fetcher", consumer = "taifex"
    }
  }
}
data "aws_iam_policy_document" "runtime_kms" {
  for_each = local.units
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
    sid       = "InstanceDecrypt"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.instance[each.key].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.aws_region}.amazonaws.com"]
    }

  }
}
resource "aws_kms_key" "runtime" {
  for_each                = local.units
  description             = "${each.key} production runtime secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.runtime_kms[each.key].json
  tags = merge(local.common_tags, {
    DeploymentUnit = each.key
  })
  lifecycle {
    prevent_destroy = true
  }
}
resource "aws_kms_alias" "runtime" {
  for_each      = local.units
  name          = "alias/findb-production-${each.key}-runtime-secrets"
  target_key_id = aws_kms_key.runtime[each.key].key_id
}
resource "aws_secretsmanager_secret" "runtime" {
  for_each                = local.secret_catalog
  name                    = "findb/production/${each.key}"
  description             = "Production ${each.value.consumer} secret metadata; value is loaded out-of-band"
  kms_key_id              = aws_kms_key.runtime[each.value.unit].arn
  recovery_window_in_days = 30
  tags = merge(local.common_tags, {
    DeploymentUnit = each.value.unit, Consumer = each.value.consumer, RuntimeStatus = "active"
  })
  lifecycle {
    prevent_destroy = true
  }
}

# Retired credential metadata is retained for rollback audit; no deployed consumer reads it.
resource "aws_secretsmanager_secret" "retired_calendar" {
  name                    = "findb/production/fetcher/api/calendar-serve"
  description             = "Retired Fetcher calendar credential; unified runtime uses Source execution policy"
  kms_key_id              = aws_kms_key.runtime["fetcher"].arn
  recovery_window_in_days = 30
  tags                    = merge(local.common_tags, { DeploymentUnit = "fetcher", Consumer = "retired", RuntimeStatus = "retired" })
  lifecycle { prevent_destroy = true }
}
moved {
  from = aws_secretsmanager_secret.runtime["fetcher/api/calendar-serve"]
  to   = aws_secretsmanager_secret.retired_calendar
}
