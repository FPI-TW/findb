locals {
  operational_alert_topic_name = "findb-staging-operational-alerts"
  operational_alert_topic_arn  = "arn:${data.aws_partition.current.partition}:sns:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${local.operational_alert_topic_name}"

  # Amazon SNS topic resource policies reject service-wide wildcards. Keep the
  # TLS deny restricted to the complete set of topic-policy actions AWS
  # documents as supported instead.
  operational_alert_topic_policy_actions = [
    "sns:AddPermission",
    "sns:DeleteTopic",
    "sns:GetDataProtectionPolicy",
    "sns:GetTopicAttributes",
    "sns:ListSubscriptionsByTopic",
    "sns:ListTagsForResource",
    "sns:Publish",
    "sns:PutDataProtectionPolicy",
    "sns:RemovePermission",
    "sns:SetTopicAttributes",
    "sns:Subscribe",
  ]

  # Keep the alarm identity policy inputs local and deterministic. These names
  # deliberately mirror the two resources below without referencing them, so
  # topic/key policies can be created before any alarm without a dependency
  # cycle.
  operational_alarm_names = concat(
    [for unit in sort(keys(local.unit_config)) : "findb-staging-${unit}-status-check-failed"],
    [for alarm_key in sort(keys(local.rds_monitoring_alarms)) : "findb-staging-rds-${replace(alarm_key, "_", "-")}"],
  )
  operational_alarm_arns = [
    for alarm_name in local.operational_alarm_names :
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:${alarm_name}"
  ]

  monitoring_alarm_common = {
    period              = var.monitoring_alarm_period_seconds
    evaluation_periods  = var.monitoring_alarm_evaluation_periods
    datapoints_to_alarm = var.monitoring_alarm_datapoints_to_alarm
    treat_missing_data  = var.monitoring_alarm_treat_missing_data
  }

  rds_monitoring_alarms = {
    free_storage_space = {
      metric_name         = "FreeStorageSpace"
      comparison_operator = "LessThanOrEqualToThreshold"
      threshold           = var.monitoring_rds_free_storage_space_threshold_bytes
      statistic           = "Average"
      unit                = "Bytes"
      description         = "fin-db has at most 5 GiB of free allocated storage for two consecutive five-minute periods."
    }
    database_connections = {
      metric_name         = "DatabaseConnections"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = var.monitoring_rds_database_connections_threshold
      statistic           = "Average"
      unit                = "Count"
      description         = "fin-db averages at least 80 database connections for two consecutive five-minute periods."
    }
    read_latency = {
      metric_name         = "ReadLatency"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = var.monitoring_rds_latency_threshold_seconds
      statistic           = "Average"
      unit                = "Seconds"
      description         = "fin-db average read latency is at least 100 ms for two consecutive five-minute periods."
    }
    write_latency = {
      metric_name         = "WriteLatency"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = var.monitoring_rds_latency_threshold_seconds
      statistic           = "Average"
      unit                = "Seconds"
      description         = "fin-db average write latency is at least 100 ms for two consecutive five-minute periods."
    }
  }
}

data "aws_iam_policy_document" "operational_alerts_kms" {
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

  # Amazon SNS encrypts the stored topic messages. Both the source account and
  # the SNS encryption context pin this grant to the one private topic.
  statement {
    sid    = "SnsEncryptOperationalAlerts"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [local.operational_alert_topic_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:aws:sns:topicArn"
      values   = [local.operational_alert_topic_arn]
    }
  }

  # CloudWatch is the SNS publisher for these alarms and needs the KMS data
  # key/decrypt permissions required for encrypted SNS event sources.
  statement {
    sid    = "CloudWatchPublishEncryptedOperationalAlerts"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = local.operational_alarm_arns
    }
  }
}

resource "aws_kms_key" "operational_alerts" {
  description             = "FinDB staging encrypted operational-alert SNS topic"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.operational_alerts_kms.json
  tags                    = merge(local.common_tags, { DeploymentUnit = "operational-alerts" })
}

resource "aws_kms_alias" "operational_alerts" {
  name          = "alias/findb-staging-operational-alerts"
  target_key_id = aws_kms_key.operational_alerts.key_id
}

resource "aws_sns_topic" "operational_alerts" {
  name              = local.operational_alert_topic_name
  display_name      = "FinDB staging operational alerts"
  kms_master_key_id = aws_kms_key.operational_alerts.arn
  tags              = merge(local.common_tags, { DeploymentUnit = "operational-alerts" })
}

data "aws_iam_policy_document" "operational_alerts_topic" {
  statement {
    sid    = "AccountRootTopicAdministrationAndSubscriptionLifecycle"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    # Publishing is intentionally absent. The only permitted publishing path
    # is the CloudWatch service statement below, constrained to this account's
    # six declared alarm ARNs. Keep the owner statement to the topic and email
    # subscription lifecycle actions Terraform and an operator need.
    actions = [
      "sns:AddPermission",
      "sns:DeleteTopic",
      "sns:GetTopicAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:RemovePermission",
      "sns:SetTopicAttributes",
      "sns:Subscribe",
    ]
    resources = [aws_sns_topic.operational_alerts.arn]
  }

  statement {
    sid    = "AllowCloudWatchAlarmPublish"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operational_alerts.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = local.operational_alarm_arns
    }
  }

  # A topic policy Allow alone cannot prevent a same-account identity policy
  # from granting direct Publish. These explicit denies make the CloudWatch
  # service plus the exact provenance checks below the exclusive publish path.
  # The negated conditions also evaluate to true when their context key is
  # absent, so a missing service/source context is denied rather than allowed.
  statement {
    sid    = "DenyPublishUnlessCloudWatchService"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operational_alerts.arn]

    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalServiceName"
      values   = ["cloudwatch.amazonaws.com"]
    }
  }

  statement {
    sid    = "DenyCloudWatchPublishFromUnexpectedAccount"
    effect = "Deny"

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operational_alerts.arn]

    condition {
      test     = "StringNotEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid    = "DenyCloudWatchPublishFromUnexpectedAlarm"
    effect = "Deny"

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operational_alerts.arn]

    condition {
      test     = "ArnNotEquals"
      variable = "aws:SourceArn"
      values   = local.operational_alarm_arns
    }
  }

  # This topic has no HTTP subscription, but denying insecure API transport
  # keeps every allowed topic API operation TLS-only as well.
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = local.operational_alert_topic_policy_actions
    resources = [aws_sns_topic.operational_alerts.arn]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sns_topic_policy" "operational_alerts" {
  arn    = aws_sns_topic.operational_alerts.arn
  policy = data.aws_iam_policy_document.operational_alerts_topic.json
}

resource "aws_sns_topic_subscription" "operational_alert_email" {
  topic_arn = aws_sns_topic.operational_alerts.arn
  protocol  = "email"
  endpoint  = var.operational_alert_email

  # PR refresh plans intentionally use the tracked example endpoint while the
  # encrypted remote state can hold the confirmed operational inbox. A
  # replacement is an explicit reviewed operator action; see the runbook.
  lifecycle {
    ignore_changes = [endpoint]
  }

  depends_on = [aws_sns_topic_policy.operational_alerts]
}

resource "aws_cloudwatch_metric_alarm" "ec2_status_check_failed" {
  for_each = local.unit_config

  depends_on = [aws_sns_topic_policy.operational_alerts]

  alarm_name          = "findb-staging-${each.key}-status-check-failed"
  alarm_description   = "${each.key} EC2 StatusCheckFailed is at least one for two consecutive five-minute periods."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = each.value.instance_id }
  statistic           = "Maximum"
  unit                = "Count"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.monitoring_ec2_status_check_failed_threshold

  period                    = local.monitoring_alarm_common.period
  evaluation_periods        = local.monitoring_alarm_common.evaluation_periods
  datapoints_to_alarm       = local.monitoring_alarm_common.datapoints_to_alarm
  treat_missing_data        = local.monitoring_alarm_common.treat_missing_data
  actions_enabled           = true
  alarm_actions             = [aws_sns_topic.operational_alerts.arn]
  ok_actions                = []
  insufficient_data_actions = []

  tags = merge(local.common_tags, { DeploymentUnit = each.key })
}

resource "aws_cloudwatch_metric_alarm" "rds_native" {
  for_each = local.rds_monitoring_alarms

  depends_on = [aws_sns_topic_policy.operational_alerts]

  alarm_name          = "findb-staging-rds-${replace(each.key, "_", "-")}"
  alarm_description   = each.value.description
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric_name
  dimensions          = { DBInstanceIdentifier = var.findb_rds_instance_identifier }
  statistic           = each.value.statistic
  unit                = each.value.unit
  comparison_operator = each.value.comparison_operator
  threshold           = each.value.threshold

  period                    = local.monitoring_alarm_common.period
  evaluation_periods        = local.monitoring_alarm_common.evaluation_periods
  datapoints_to_alarm       = local.monitoring_alarm_common.datapoints_to_alarm
  treat_missing_data        = local.monitoring_alarm_common.treat_missing_data
  actions_enabled           = true
  alarm_actions             = [aws_sns_topic.operational_alerts.arn]
  ok_actions                = []
  insufficient_data_actions = []

  tags = merge(local.common_tags, { DeploymentUnit = "findb-rds" })
}
