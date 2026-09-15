locals {
  root_volume_backup_selection = "findb-staging-current-root-daily"
  root_volume_config = {
    findb = {
      volume_id = one(data.aws_instance.findb.root_block_device).volume_id
    }
    fetcher = {
      volume_id = one(data.aws_instance.fetcher.root_block_device).volume_id
    }
  }
}

# Keep selection bound to the root volumes currently attached to the two
# reviewed staging instances. When a root volume is replaced, OpenTofu moves
# this unique tag to the replacement rather than leaving a historical volume
# in the recurring chain.
resource "aws_ec2_tag" "root_volume_backup_selection" {
  for_each = local.root_volume_config

  resource_id = each.value.volume_id
  key         = "FinDBBackupPolicy"
  value       = local.root_volume_backup_selection
}

data "aws_iam_policy_document" "dlm_assume_role" {
  statement {
    sid     = "DataLifecycleManagerOnly"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm_root_volume_backup" {
  name                 = "findb-staging-dlm-root-volume-backup"
  assume_role_policy   = data.aws_iam_policy_document.dlm_assume_role.json
  max_session_duration = 3600
  tags                 = merge(local.common_tags, { DeploymentUnit = "backup" })
}

resource "aws_iam_role_policy_attachment" "dlm_root_volume_backup" {
  role       = aws_iam_role.dlm_root_volume_backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "root_volume_backup" {
  description        = "Daily encrypted recovery points for the current FinDB staging root volumes"
  execution_role_arn = aws_iam_role.dlm_root_volume_backup.arn
  state              = "ENABLED"
  tags               = merge(local.common_tags, { DeploymentUnit = "backup" })

  policy_details {
    policy_type    = "EBS_SNAPSHOT_MANAGEMENT"
    resource_types = ["VOLUME"]
    target_tags = {
      FinDBBackupPolicy = local.root_volume_backup_selection
    }

    schedule {
      name      = "DailyCurrentRootRecoveryPoints"
      copy_tags = true
      tags_to_add = {
        # Source volumes already carry Purpose. DLM rejects duplicate keys
        # when CopyTags is enabled, so use a schedule-only, non-overlapping key.
        BackupPurpose = "automated-current-root-backup"
        BackupOwner   = var.backup_owner_tag
      }

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["09:00"]
      }

      retain_rule {
        count = 7
      }
    }
  }

  depends_on = [
    aws_ec2_tag.root_volume_backup_selection,
    aws_iam_role_policy_attachment.dlm_root_volume_backup,
  ]
}
