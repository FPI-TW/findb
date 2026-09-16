data "aws_iam_policy_document" "dlm_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "dlm" {
  name               = "findb-production-dlm"
  assume_role_policy = data.aws_iam_policy_document.dlm_trust.json
  tags = merge(local.common_tags, {
    DeploymentUnit = "backup"
  })
}
resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}
resource "aws_dlm_lifecycle_policy" "root" {
  description        = "Daily production root-volume snapshots"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"
  tags = merge(local.common_tags, {
    DeploymentUnit = "backup"
  })
  policy_details {
    policy_type    = "EBS_SNAPSHOT_MANAGEMENT"
    resource_types = ["VOLUME"]
    target_tags = {
      FinDBBackupPolicy = "findb-production-root-daily"
    }
    schedule {
      name      = "DailyRootRecoveryPoints"
      copy_tags = true
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["09:00"]
      }
      retain_rule {
        count = 14
      }
      tags_to_add = {
        BackupOwner = var.backup_owner, BackupPurpose = "automated-root-backup"
      }

    }

  }
}
