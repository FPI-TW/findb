terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project     = var.project_tag
      Environment = var.environment_tag
      Owner       = var.owner_tag
    }
  }
}

data "aws_partition" "current" {}

data "aws_caller_identity" "current" {}

data "aws_iam_openid_connect_provider" "github_existing" {
  count = var.github_oidc_provider_arn != "" ? 1 : 0
  arn   = var.github_oidc_provider_arn
}

data "aws_instance" "findb" {
  instance_id = var.findb_instance_id
}

data "aws_instance" "fetcher" {
  instance_id = var.fetcher_instance_id
}
