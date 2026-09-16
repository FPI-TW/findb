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
    [for alarm_key in sort(keys(local.custom_monitoring_alarms)) : local.custom_monitoring_alarms[alarm_key].alarm_name],
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

  monitored_containers = {
    findb = toset([
      "findb-dashboard",
      "findb-dispatcher",
      "findb-ingest",
      "findb-nginx",
      "findb-rabbitmq",
      "findb-raw-cleanup",
      "findb-serve",
      "findb-worker",
    ])
    fetcher = toset([
      "findb-fetcher-finlab-scheduler",
      "findb-fetcher-scheduler",
      "findb-fetcher-shioaji-scheduler",
    ])
  }

  scheduler_keys = toset([
    "finlab_tw_equity_eod_v1",
    "shioaji_tw_pilot_v1",
    "twelve_data_us_common_stocks_daily_v1",
  ])

  metric_publisher_service_units = {
    for unit in sort(keys(local.unit_config)) : unit => <<-EOT
      [Unit]
      Description=Publish FinDB staging operational metrics for ${unit}
      After=docker.service network-online.target
      Wants=network-online.target

      [Service]
      Type=oneshot
      ExecStart=/usr/bin/python3 /usr/local/lib/findb-monitoring/publish_staging_metrics.py --unit ${unit} --region ${var.aws_region} --rds-instance-identifier ${var.findb_rds_instance_identifier} --dlm-policy-id ${aws_dlm_lifecycle_policy.root_volume_backup.id} --tls-host ${var.findb_dns_check_name}
    EOT
  }

  metric_publisher_timer_unit = <<-EOT
    [Unit]
    Description=Publish FinDB staging operational metrics every five minutes

    [Timer]
    OnBootSec=1min
    OnUnitActiveSec=5min
    AccuracySec=30s
    Persistent=true
    Unit=findb-staging-metric-publisher.service

    [Install]
    WantedBy=timers.target
  EOT

  custom_monitoring_alarms = merge(
    {
      for unit in sort(keys(local.unit_config)) : "${unit}_disk" => {
        alarm_name          = "findb-staging-${unit}-disk-used"
        metric_name         = "DiskUsedPercent"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = var.monitoring_disk_used_percent_threshold
        unit                = "Percent"
        deployment_unit     = unit
        resource            = "/"
        evaluation_periods  = 2
        datapoints_to_alarm = 2
        treat_missing_data  = "breaching"
        description         = "${unit} root filesystem usage is at least 85 percent or the host metric is missing."
      }
    },
    {
      for unit in sort(keys(local.unit_config)) : "${unit}_inode" => {
        alarm_name          = "findb-staging-${unit}-inode-used"
        metric_name         = "InodeUsedPercent"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = var.monitoring_inode_used_percent_threshold
        unit                = "Percent"
        deployment_unit     = unit
        resource            = "/"
        evaluation_periods  = 2
        datapoints_to_alarm = 2
        treat_missing_data  = "breaching"
        description         = "${unit} root filesystem inode usage is at least 90 percent or the host metric is missing."
      }
    },
    {
      for unit in sort(keys(local.unit_config)) : "${unit}_collector" => {
        alarm_name          = "findb-staging-${unit}-metric-collector-failed"
        metric_name         = "CollectorSuccess"
        comparison_operator = "LessThanThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = unit
        resource            = "host"
        evaluation_periods  = 2
        datapoints_to_alarm = 2
        treat_missing_data  = "breaching"
        description         = "${unit} metric collection failed or stopped publishing for two consecutive periods."
      }
    },
    merge([
      for unit, containers in local.monitored_containers : {
        for container in containers : "${unit}_container_health_${container}" => {
          alarm_name          = "findb-staging-${unit}-${replace(container, "findb-", "")}-unhealthy"
          metric_name         = "DockerContainerHealthy"
          comparison_operator = "LessThanThreshold"
          threshold           = 1
          unit                = "Count"
          deployment_unit     = unit
          resource            = container
          evaluation_periods  = 2
          datapoints_to_alarm = 2
          treat_missing_data  = "breaching"
          description         = "${container} is absent, stopped, unhealthy, or its health metric is missing."
        }
      }
    ]...),
    merge([
      for unit, containers in local.monitored_containers : {
        for container in containers : "${unit}_container_restart_${container}" => {
          alarm_name          = "findb-staging-${unit}-${replace(container, "findb-", "")}-restarted"
          metric_name         = "DockerRestartCount"
          comparison_operator = "GreaterThanOrEqualToThreshold"
          threshold           = var.monitoring_docker_restart_count_threshold
          unit                = "Count"
          deployment_unit     = unit
          resource            = container
          evaluation_periods  = 1
          datapoints_to_alarm = 1
          treat_missing_data  = "notBreaching"
          description         = "${container} reports at least three Docker restarts in its current container lifetime."
        }
      }
    ]...),
    {
      for container in local.monitored_containers.fetcher : "fetcher_runtime_security_${container}" => {
        alarm_name          = "findb-staging-fetcher-${replace(container, "findb-fetcher-", "")}-runtime-security"
        metric_name         = "DockerRuntimeSecurityHealthy"
        comparison_operator = "LessThanThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "fetcher"
        resource            = container
        evaluation_periods  = 2
        datapoints_to_alarm = 2
        treat_missing_data  = "breaching"
        description         = "${container} violates its non-root, read-only, capability, privilege, tmpfs, or exact writable-mount contract, or its metric is missing."
      }
    },
    {
      rabbitmq_disk = {
        alarm_name          = "findb-staging-rabbitmq-disk-alarm"
        metric_name         = "RabbitMQDiskAlarm"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "findb-rabbitmq"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "breaching"
        description         = "RabbitMQ reports a local disk alarm or the metric is missing."
      }
      rabbitmq_memory = {
        alarm_name          = "findb-staging-rabbitmq-memory-alarm"
        metric_name         = "RabbitMQMemoryAlarm"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "findb-rabbitmq"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "breaching"
        description         = "RabbitMQ reports a local memory alarm or the metric is missing."
      }
      rds_backup_lag = {
        alarm_name          = "findb-staging-rds-backup-lag"
        metric_name         = "RDSBackupLagSeconds"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = var.monitoring_rds_backup_lag_threshold_seconds
        unit                = "Seconds"
        deployment_unit     = "findb"
        resource            = var.findb_rds_instance_identifier
        evaluation_periods  = 2
        datapoints_to_alarm = 2
        treat_missing_data  = "breaching"
        description         = "fin-db latest restorable time lags by at least 30 minutes or the metric is missing."
      }
      dlm_policy_health = {
        alarm_name          = "findb-staging-dlm-policy-unhealthy"
        metric_name         = "DLMPolicyHealthy"
        comparison_operator = "LessThanThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = aws_dlm_lifecycle_policy.root_volume_backup.id
        evaluation_periods  = 2
        datapoints_to_alarm = 2
        treat_missing_data  = "breaching"
        description         = "The staging root-volume DLM policy is disabled, reports an error, or its health metric is missing."
      }
      tls_certificate_expiry = {
        alarm_name          = "findb-staging-tls-certificate-expiring"
        metric_name         = "TLSCertificateDaysRemaining"
        comparison_operator = "LessThanOrEqualToThreshold"
        threshold           = var.monitoring_tls_certificate_days_remaining_threshold
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = var.findb_dns_check_name
        evaluation_periods  = 2
        datapoints_to_alarm = 2
        treat_missing_data  = "breaching"
        description         = "The verified public staging TLS certificate expires within 30 days, cannot be verified, or its metric is missing."
      }
      active_feed_rejected_attempt = {
        alarm_name          = "findb-staging-active-feed-ingress-rejected"
        metric_name         = "ActiveFeedRejectedAttempts"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "active-feeds"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "notBreaching"
        description         = "An active staging feed was rejected for an ingress schema, required-field, or completeness-policy violation in the overlapping ten-minute observation window."
      }
      active_feed_empty_snapshot = {
        alarm_name          = "findb-staging-active-feed-empty-snapshot"
        metric_name         = "ActiveFeedEmptySnapshots"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "active-feeds"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "notBreaching"
        description         = "An active staging feed persisted a non-rerun ingestion run with zero records in the overlapping ten-minute observation window."
      }
      active_feed_dq_error = {
        alarm_name          = "findb-staging-active-feed-dq-error"
        metric_name         = "ActiveFeedDQErrors"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "active-feeds"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "notBreaching"
        description         = "An active staging feed produced a blocking DQ error in the overlapping ten-minute observation window."
      }
      lineage_orphans = {
        alarm_name          = "findb-staging-lineage-orphan-rows"
        metric_name         = "LineageOrphanRows"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "canonical-and-raw"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "breaching"
        description         = "At least one canonical or PostgreSQL raw row references a missing ingestion run, or the integrity metric is missing."
      }
      eod_default_partition = {
        alarm_name          = "findb-staging-eod-default-partition-nonempty"
        metric_name         = "EODDefaultPartitionRows"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "market_data_eod_default"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "breaching"
        description         = "The migration-owned EOD default partition contains at least one row, or its metric is missing."
      }
      credential_usage_aggregate_mismatch = {
        alarm_name          = "findb-staging-credential-usage-aggregate-mismatch"
        metric_name         = "CredentialUsageAggregateMismatches"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "api-credentials"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "breaching"
        description         = "At least one durable credential usage rollup differs from its credential aggregate, or the metric is missing."
      }
      invalid_credential_events = {
        alarm_name          = "findb-staging-invalid-credential-events"
        metric_name         = "InvalidCredentialEvents"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = "findb"
        resource            = "api-credentials"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "notBreaching"
        description         = "At least one API credential was rejected in the overlapping ten-minute Docker log window."
      }
    },
    {
      for scheduler_key in local.scheduler_keys : "scheduler_${scheduler_key}" => {
        alarm_name          = "findb-staging-fetcher-${replace(scheduler_key, "_", "-")}-heartbeat-stale"
        metric_name         = "SchedulerHeartbeatAgeSeconds"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = var.monitoring_scheduler_heartbeat_age_threshold_seconds
        unit                = "Seconds"
        deployment_unit     = "fetcher"
        resource            = scheduler_key
        evaluation_periods  = 2
        datapoints_to_alarm = 2
        treat_missing_data  = "breaching"
        description         = "${scheduler_key} heartbeat is at least three minutes old or the metric is missing."
      }
    },
    {
      for unit in sort(keys(local.unit_config)) : "${unit}_deployment_failure" => {
        alarm_name          = "findb-staging-${unit}-deployment-failed"
        metric_name         = "DeploymentFailure"
        comparison_operator = "GreaterThanOrEqualToThreshold"
        threshold           = 1
        unit                = "Count"
        deployment_unit     = unit
        resource            = "github-actions"
        evaluation_periods  = 1
        datapoints_to_alarm = 1
        treat_missing_data  = "notBreaching"
        description         = "The ${unit} staging CD workflow published a deployment failure."
      }
    },
  )
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

resource "aws_cloudwatch_metric_alarm" "custom" {
  for_each = local.custom_monitoring_alarms

  # The first association executions seed the non-sparse metrics before alarms
  # with treat_missing_data=breaching can notify.
  depends_on = [
    aws_sns_topic_policy.operational_alerts,
    aws_ssm_association.staging_metric_publisher,
  ]

  alarm_name          = each.value.alarm_name
  alarm_description   = each.value.description
  namespace           = "FinDB/Staging"
  metric_name         = each.value.metric_name
  dimensions          = { DeploymentUnit = each.value.deployment_unit, Resource = each.value.resource }
  statistic           = "Maximum"
  unit                = each.value.unit
  comparison_operator = each.value.comparison_operator
  threshold           = each.value.threshold

  period                    = var.monitoring_alarm_period_seconds
  evaluation_periods        = each.value.evaluation_periods
  datapoints_to_alarm       = each.value.datapoints_to_alarm
  treat_missing_data        = each.value.treat_missing_data
  actions_enabled           = true
  alarm_actions             = [aws_sns_topic.operational_alerts.arn]
  ok_actions                = []
  insufficient_data_actions = []

  tags = merge(local.common_tags, { DeploymentUnit = each.value.deployment_unit })
}

resource "aws_ssm_association" "staging_metric_publisher" {
  for_each = local.unit_config

  association_name = "findb-staging-${each.key}-metric-publisher"
  name             = "AWS-RunShellScript"
  # State Manager associations have a 30-minute minimum interval. Reconcile
  # the local systemd timer at that supported interval; the timer publishes
  # every five minutes so the alarm contract retains its two-period window.
  schedule_expression              = "rate(30 minutes)"
  apply_only_at_cron_interval      = false
  compliance_severity              = "HIGH"
  max_concurrency                  = "1"
  max_errors                       = "0"
  wait_for_success_timeout_seconds = 300

  depends_on = [aws_iam_role_policy.instance_permissions]

  parameters = {
    commands = join("\n", [
      "set -eu",
      "install -d -o root -g root -m 0755 /usr/local/lib/findb-monitoring",
      "printf '%s' '${filebase64("${path.module}/../../monitoring/publish_staging_metrics.py")}' | base64 -d > /usr/local/lib/findb-monitoring/publish_staging_metrics.py.tmp",
      "chown root:root /usr/local/lib/findb-monitoring/publish_staging_metrics.py.tmp",
      "chmod 0755 /usr/local/lib/findb-monitoring/publish_staging_metrics.py.tmp",
      "mv -f /usr/local/lib/findb-monitoring/publish_staging_metrics.py.tmp /usr/local/lib/findb-monitoring/publish_staging_metrics.py",
      "printf '%s' '${base64encode(local.metric_publisher_service_units[each.key])}' | base64 -d > /etc/systemd/system/findb-staging-metric-publisher.service.tmp",
      "chown root:root /etc/systemd/system/findb-staging-metric-publisher.service.tmp",
      "chmod 0644 /etc/systemd/system/findb-staging-metric-publisher.service.tmp",
      "mv -f /etc/systemd/system/findb-staging-metric-publisher.service.tmp /etc/systemd/system/findb-staging-metric-publisher.service",
      "printf '%s' '${base64encode(local.metric_publisher_timer_unit)}' | base64 -d > /etc/systemd/system/findb-staging-metric-publisher.timer.tmp",
      "chown root:root /etc/systemd/system/findb-staging-metric-publisher.timer.tmp",
      "chmod 0644 /etc/systemd/system/findb-staging-metric-publisher.timer.tmp",
      "mv -f /etc/systemd/system/findb-staging-metric-publisher.timer.tmp /etc/systemd/system/findb-staging-metric-publisher.timer",
      "systemctl daemon-reload",
      "systemctl enable --now findb-staging-metric-publisher.timer",
      "systemctl start findb-staging-metric-publisher.service",
      "systemctl is-active --quiet findb-staging-metric-publisher.timer",
    ])
  }

  targets {
    key    = "InstanceIds"
    values = [each.value.instance_id]
  }

  tags = merge(local.common_tags, { DeploymentUnit = each.key })
}
