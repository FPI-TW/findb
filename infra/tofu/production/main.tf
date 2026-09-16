locals {
  common_tags = {
    Project = "findb", Environment = "production", DeploymentUnit = "control-plane", Owner = var.owner, BackupOwner = var.backup_owner
  }
  units = {
    findb = {
      instance_type = "t3.large", subnet = "public_a", environment = "production-findb"
      role_prefix   = "findb"
    }
    fetcher = {
      instance_type = "t3.medium", subnet = "public_c", environment = "production-fetcher"
      role_prefix   = "fetcher"
    }

  }
  repositories = {
    findb_backend = {
      name = "findb/production/backend", unit = "findb"
    }
    findb_dashboard = {
      name = "findb/production/dashboard", unit = "findb"
    }
    fetcher_twelve_data = {
      name = "findb/production/fetcher/twelve-data", unit = "fetcher"
    }
    fetcher_finlab = {
      name = "findb/production/fetcher/finlab", unit = "fetcher"
    }
    fetcher_shioaji = {
      name = "findb/production/fetcher/shioaji", unit = "fetcher"
    }

  }
  repositories_by_unit = {
    findb   = [for _, r in local.repositories : r.name if r.unit == "findb"]
    fetcher = [for _, r in local.repositories : r.name if r.unit == "fetcher"]

  }
}

resource "terraform_data" "account_guard" {
  input = data.aws_caller_identity.current.account_id
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.aws_account_id
      error_message = "Wrong AWS account for Production."
    }
  }
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
  tags = merge(local.common_tags, {
    DeploymentUnit = "github-oidc"
  })
  lifecycle {
    prevent_destroy = true
  }
}
