resource "aws_sns_topic" "operations" {
  name              = "findb-production-operational-alerts"
  kms_master_key_id = aws_kms_key.logs.id
  tags = merge(local.common_tags, {
    DeploymentUnit = "monitoring"
  })
}
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.operations.arn
  protocol  = "email"
  endpoint  = var.operational_alert_email
}

locals {
  ec2_alarms = {
    for unit in keys(local.units) : unit => {
      instance_id = aws_instance.unit[unit].id, name = "findb-production-${unit}-status-check"
    }

  }
  monitored_containers = {
    findb   = toset(["findb-dashboard", "findb-dispatcher", "findb-ingest", "findb-nginx", "findb-rabbitmq", "findb-raw-cleanup", "findb-serve", "findb-worker"])
    fetcher = toset(["findb-fetcher-finlab-scheduler", "findb-fetcher-scheduler", "findb-fetcher-shioaji-scheduler"])
  }
  scheduler_keys = toset(["twelve_data_us_common_stocks_daily_v1", "finlab_tw_equity_eod_v1", "shioaji_tw_pilot_v1"])
  custom_alarms = merge(
    {
      for unit in keys(local.units) : "${unit}_disk" => { unit = unit, metric = "DiskUsedPercent", resource = "/", op = "GreaterThanOrEqualToThreshold", threshold = 85 }
    },
    {
      for unit in keys(local.units) : "${unit}_inode" => { unit = unit, metric = "InodeUsedPercent", resource = "/", op = "GreaterThanOrEqualToThreshold", threshold = 90 }
    },
    {
      for unit in keys(local.units) : "${unit}_collector" => { unit = unit, metric = "CollectorSuccess", resource = "host", op = "LessThanThreshold", threshold = 1 }
    },
    merge([for unit, containers in local.monitored_containers : {
      for container in containers : "${unit}_${container}_health" => { unit = unit, metric = "DockerContainerHealthy", resource = container, op = "LessThanThreshold", threshold = 1 }
    }]...),
    merge([for unit, containers in local.monitored_containers : {
      for container in containers : "${unit}_${container}_restart" => { unit = unit, metric = "DockerRestartCount", resource = container, op = "GreaterThanOrEqualToThreshold", threshold = 3 }
    }]...),
    {
      for key in local.scheduler_keys : "scheduler_${key}_heartbeat" => { unit = "fetcher", metric = "SchedulerHeartbeatAgeSeconds", resource = key, op = "GreaterThanOrEqualToThreshold", threshold = 180 }
    },
    {
      for key in local.scheduler_keys : "scheduler_${key}_freshness" => { unit = "fetcher", metric = "FeedFreshnessHealthy", resource = key, op = "LessThanThreshold", threshold = 1 }
    },
    {
      rabbitmq_disk        = { unit = "findb", metric = "RabbitMQDiskAlarm", resource = "findb-rabbitmq", op = "GreaterThanOrEqualToThreshold", threshold = 1 }
      rabbitmq_memory      = { unit = "findb", metric = "RabbitMQMemoryAlarm", resource = "findb-rabbitmq", op = "GreaterThanOrEqualToThreshold", threshold = 1 }
      tls                  = { unit = "findb", metric = "TLSCertificateDaysRemaining", resource = "findb.tingfong.com", op = "LessThanOrEqualToThreshold", threshold = 30 }
      rds_backup_lag       = { unit = "findb", metric = "RDSBackupLagSeconds", resource = aws_db_instance.production.identifier, op = "GreaterThanOrEqualToThreshold", threshold = 1800 }
      dlm                  = { unit = "findb", metric = "DLMPolicyHealthy", resource = aws_dlm_lifecycle_policy.root.id, op = "LessThanThreshold", threshold = 1 }
      rejected_credentials = { unit = "findb", metric = "InvalidCredentialEvents", resource = "api-credentials", op = "GreaterThanOrEqualToThreshold", threshold = 1 }
      active_feed_rejected = { unit = "findb", metric = "ActiveFeedRejectedAttempts", resource = "active-feeds", op = "GreaterThanOrEqualToThreshold", threshold = 1 }
      active_feed_empty    = { unit = "findb", metric = "ActiveFeedEmptySnapshots", resource = "active-feeds", op = "GreaterThanOrEqualToThreshold", threshold = 1 }
      active_feed_dq       = { unit = "findb", metric = "ActiveFeedDQErrors", resource = "active-feeds", op = "GreaterThanOrEqualToThreshold", threshold = 1 }
      deployment_failure   = { unit = "control-plane", metric = "DeploymentFailure", resource = "all", op = "GreaterThanOrEqualToThreshold", threshold = 1 }
    },
  )
  metric_service = {
    for unit in keys(local.units) : unit => <<-EOT
      [Unit]
      Description=Publish FinDB production operational metrics for ${unit}
      After=docker.service network-online.target

      [Service]
      Type=oneshot
      ExecStart=/usr/bin/python3 /usr/local/lib/findb-monitoring/publish_metrics.py --unit ${unit} --region ${var.aws_region} --namespace FinDB/Production --rds-instance-identifier ${aws_db_instance.production.identifier} --dlm-policy-id ${aws_dlm_lifecycle_policy.root.id} --tls-host findb.tingfong.com
    EOT
  }
  metric_timer = <<-EOT
    [Unit]
    Description=Publish FinDB production metrics every five minutes

    [Timer]
    OnBootSec=1min
    OnUnitActiveSec=5min
    AccuracySec=30s
    Persistent=true
    Unit=findb-production-metric-publisher.service

    [Install]
    WantedBy=timers.target
  EOT
}
resource "aws_cloudwatch_metric_alarm" "ec2" {
  for_each            = local.ec2_alarms
  alarm_name          = each.value.name
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"
  dimensions = {
    InstanceId = each.value.instance_id
  }
  alarm_actions = [aws_sns_topic.operations.arn]
  ok_actions    = [aws_sns_topic.operations.arn]
}
resource "aws_cloudwatch_metric_alarm" "ec2_cpu" {
  for_each            = local.ec2_alarms
  alarm_name          = "findb-production-${each.key}-cpu-high"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 85
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "missing"
  dimensions = {
    InstanceId = each.value.instance_id
  }
  alarm_actions = [aws_sns_topic.operations.arn]
  ok_actions    = [aws_sns_topic.operations.arn]
}
resource "aws_cloudwatch_metric_alarm" "rds" {
  for_each = {
    cpu = {
      metric = "CPUUtilization", op = "GreaterThanOrEqualToThreshold", threshold = 85, unit = "Percent"
    }
    storage = {
      metric = "FreeStorageSpace", op = "LessThanOrEqualToThreshold", threshold = 10737418240, unit = "Bytes"
    }
    connections = {
      metric = "DatabaseConnections", op = "GreaterThanOrEqualToThreshold", threshold = 80, unit = "Count"
    }

  }
  alarm_name          = "findb-production-rds-${each.key}"
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  statistic           = "Average"
  unit                = each.value.unit
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = each.value.threshold
  comparison_operator = each.value.op
  treat_missing_data  = "missing"
  dimensions = {
    DBInstanceIdentifier = aws_db_instance.production.identifier
  }
  alarm_actions = [aws_sns_topic.operations.arn]
  ok_actions    = [aws_sns_topic.operations.arn]
}
resource "aws_cloudwatch_metric_alarm" "custom" {
  for_each            = local.custom_alarms
  alarm_name          = "findb-production-${replace(each.key, "_", "-")}"
  namespace           = "FinDB/Production"
  metric_name         = each.value.metric
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = each.value.threshold
  comparison_operator = each.value.op
  treat_missing_data  = "breaching"
  dimensions = {
    DeploymentUnit = each.value.unit, Resource = each.value.resource
  }
  alarm_actions = [aws_sns_topic.operations.arn]
  ok_actions    = [aws_sns_topic.operations.arn]
}

resource "aws_ssm_association" "metric_publisher" {
  for_each                         = local.units
  association_name                 = "findb-production-${each.key}-metric-publisher"
  name                             = "AWS-RunShellScript"
  schedule_expression              = "rate(30 minutes)"
  apply_only_at_cron_interval      = false
  compliance_severity              = "HIGH"
  max_concurrency                  = "1"
  max_errors                       = "0"
  wait_for_success_timeout_seconds = 300
  parameters = {
    commands = join("\n", [
      "set -eu",
      "install -d -o root -g root -m 0755 /usr/local/lib/findb-monitoring",
      "printf '%s' '${filebase64("${path.module}/../../monitoring/publish_staging_metrics.py")}' | base64 -d > /usr/local/lib/findb-monitoring/publish_metrics.py.tmp",
      "chown root:root /usr/local/lib/findb-monitoring/publish_metrics.py.tmp",
      "chmod 0755 /usr/local/lib/findb-monitoring/publish_metrics.py.tmp",
      "mv -f /usr/local/lib/findb-monitoring/publish_metrics.py.tmp /usr/local/lib/findb-monitoring/publish_metrics.py",
      "printf '%s' '${base64encode(local.metric_service[each.key])}' | base64 -d > /etc/systemd/system/findb-production-metric-publisher.service",
      "printf '%s' '${base64encode(local.metric_timer)}' | base64 -d > /etc/systemd/system/findb-production-metric-publisher.timer",
      "chown root:root /etc/systemd/system/findb-production-metric-publisher.service /etc/systemd/system/findb-production-metric-publisher.timer",
      "chmod 0644 /etc/systemd/system/findb-production-metric-publisher.service /etc/systemd/system/findb-production-metric-publisher.timer",
      "systemctl daemon-reload",
      "systemctl enable --now findb-production-metric-publisher.timer",
      "systemctl start findb-production-metric-publisher.service || true",
      "systemctl is-active --quiet findb-production-metric-publisher.timer",
    ])
  }
  targets {
    key    = "InstanceIds"
    values = [aws_instance.unit[each.key].id]
  }
  tags       = merge(local.common_tags, { DeploymentUnit = each.key })
  depends_on = [aws_iam_role_policy.instance_base]
}
