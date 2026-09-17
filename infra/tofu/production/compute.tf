data "aws_iam_policy_document" "instance_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

locals {
  host_packages = "docker.io docker-compose-v2 jq python3"
}

resource "aws_iam_role" "instance" {
  for_each           = local.units
  name               = "${each.value.role_prefix}-production-instance"
  assume_role_policy = data.aws_iam_policy_document.instance_trust.json
  tags = merge(local.common_tags, {
    DeploymentUnit = each.key
  })
}
resource "aws_iam_instance_profile" "instance" {
  for_each = local.units
  name     = aws_iam_role.instance[each.key].name
  role     = aws_iam_role.instance[each.key].name
}

data "aws_iam_policy_document" "instance_base" {
  for_each = local.units
  statement {
    sid       = "SsmAgent"
    actions   = ["ssm:DescribeAssociation", "ssm:DescribeDocument", "ssm:GetDocument", "ssm:ListAssociations", "ssm:ListInstanceAssociations", "ssm:UpdateInstanceAssociationStatus", "ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel", "ec2messages:AcknowledgeMessage", "ec2messages:DeleteMessage", "ec2messages:FailMessage", "ec2messages:GetEndpoint", "ec2messages:GetMessages", "ec2messages:SendReply"]
    resources = ["*"]

  }
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ssm[each.key].arn}:*"]

  }
  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
    resources = ["arn:${data.aws_partition.current.partition}:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:findb/production/${each.key}/*"]

  }
  statement {
    sid       = "PullOwnImages"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [for name in local.repositories_by_unit[each.key] : "arn:${data.aws_partition.current.partition}:ecr:${var.aws_region}:${var.aws_account_id}:repository/${name}"]

  }
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadOwnBundles"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.deploy_bundle.arn}/${each.key}/*"]

  }
  statement {
    sid       = "DecryptOwnResources"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.deploy_bundle.arn, aws_kms_key.runtime[each.key].arn]
  }
  statement {
    sid       = "PublishOperationalMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["FinDB/Production"]
    }

  }
  statement {
    sid       = "ReadBackupHealth"
    actions   = ["dlm:GetLifecyclePolicy", "rds:DescribeDBInstances"]
    resources = ["*"]
  }
}
resource "aws_iam_role_policy" "instance_base" {
  for_each = local.units
  name     = "production-runtime"
  role     = aws_iam_role.instance[each.key].id
  policy   = data.aws_iam_policy_document.instance_base[each.key].json
}

resource "aws_cloudwatch_log_group" "ssm" {
  for_each          = local.units
  name              = "/findb/production/${each.key}/ssm"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.logs.arn
  tags = merge(local.common_tags, {
    DeploymentUnit = each.key
  })
}

resource "aws_instance" "unit" {
  for_each               = local.units
  depends_on             = [aws_iam_role_policy.instance_base]
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = each.value.instance_type
  subnet_id              = aws_subnet.production[each.value.subnet].id
  vpc_security_group_ids = [each.key == "findb" ? aws_security_group.findb.id : aws_security_group.fetcher.id]
  iam_instance_profile   = aws_iam_instance_profile.instance[each.key].name
  key_name               = null
  monitoring             = true
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }
  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = each.key == "findb" ? 50 : 30
    delete_on_termination = true
    tags = merge(local.common_tags, {
      DeploymentUnit = each.key, FinDBBackupPolicy = "findb-production-root-daily"
    })
  }
  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    snap start amazon-ssm-agent || true
    systemctl enable amazon-ssm-agent || true
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ${local.host_packages}
    snap install aws-cli --classic || snap refresh aws-cli
    systemctl enable --now docker
    install -d -m 0750 /opt/findb/releases /run/findb-runtime-secrets
    install -d -m 0750 /var/lib/findb-fetcher /var/lib/findb-finlab-fetcher/cache /var/lib/findb-shioaji-fetcher/cache
  EOT
  tags = merge(local.common_tags, {
    Name = "findb-production-${each.key}", DeploymentUnit = each.key
  })
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ssm_association" "host_dependencies" {
  for_each                         = local.units
  association_name                 = "findb-production-${each.key}-host-dependencies"
  name                             = "AWS-RunShellScript"
  apply_only_at_cron_interval      = false
  compliance_severity              = "HIGH"
  max_concurrency                  = "1"
  max_errors                       = "0"
  wait_for_success_timeout_seconds = 600
  parameters = {
    commands = join("\n", [
      "set -eu",
      "export DEBIAN_FRONTEND=noninteractive",
      "cloud-init status --wait >/dev/null || true",
      "apt-get update",
      "apt-get install -y ${local.host_packages}",
      "systemctl enable --now docker",
      "docker compose version >/dev/null",
    ])
  }
  targets {
    key    = "InstanceIds"
    values = [aws_instance.unit[each.key].id]
  }
}

resource "aws_eip" "unit" {
  for_each = local.units
  domain   = "vpc"
  instance = aws_instance.unit[each.key].id
  tags = merge(local.common_tags, {
    Name = "findb-production-${each.key}", DeploymentUnit = each.key
  })
}
