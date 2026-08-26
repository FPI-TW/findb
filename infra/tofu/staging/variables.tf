variable "aws_region" {
  description = "AWS region containing the existing staging EC2 instances."
  type        = string
  default     = "ap-southeast-1"
}

variable "aws_account_id" {
  description = "Expected AWS account ID; apply fails if the provider is pointed at another account."
  type        = string
  default     = "439622209937"
}

variable "project_tag" {
  type    = string
  default = "findb"

  validation {
    condition     = var.project_tag == "findb"
    error_message = "The Project tag contract is fixed to lowercase findb."
  }
}

variable "environment_tag" {
  type    = string
  default = "staging"
}

variable "owner_tag" {
  type    = string
  default = "tylercore"
}

variable "backup_owner_tag" {
  type    = string
  default = "tylercore"
}

variable "findb_instance_id" {
  description = "Existing FinDB staging EC2 instance ID; the instance is referenced, not recreated."
  type        = string
}

variable "fetcher_instance_id" {
  description = "Existing Fetcher staging EC2 instance ID; the instance is referenced, not recreated."
  type        = string
}

variable "github_repository" {
  description = "GitHub repository in owner/name form used by the OIDC trust policies."
  type        = string
  default     = "FPI-TW/findb"
}

variable "github_oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN. Set this when the provider is owned outside this stack."
  type        = string
  default     = ""
}

variable "manage_github_oidc_provider" {
  description = "Explicit opt-in to create the GitHub OIDC provider after inventory proves it is absent."
  type        = bool
  default     = false
}

variable "github_oidc_thumbprint" {
  description = "Root CA thumbprint used only when this stack creates the GitHub OIDC provider."
  type        = string
  default     = "6938fd4d98bab03faadb97b34396831e3780aea1"
}

variable "findb_deploy_role_name" {
  type    = string
  default = "findb-staging-deploy"
}

variable "fetcher_deploy_role_name" {
  type    = string
  default = "fetcher-staging-deploy"
}

variable "findb_instance_role_name" {
  type    = string
  default = "findb-staging-instance"
}

variable "fetcher_instance_role_name" {
  type    = string
  default = "fetcher-staging-instance"
}

variable "findb_instance_profile_name" {
  type    = string
  default = "findb-staging-instance"
}

variable "fetcher_instance_profile_name" {
  type    = string
  default = "fetcher-staging-instance"
}

variable "deploy_bundle_bucket_name" {
  description = "Private S3 control-plane bucket for versioned deployment bundles and manifests."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.deploy_bundle_bucket_name))
    error_message = "deploy_bundle_bucket_name must be a valid S3 bucket name."
  }
}

variable "deploy_bundle_kms_alias" {
  type    = string
  default = "alias/findb-staging-deploy-bundle"
}

variable "ssm_log_retention_days" {
  description = "CloudWatch retention for SSM command and Session Manager logs."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.ssm_log_retention_days)
    error_message = "Use a supported CloudWatch Logs retention period."
  }
}

variable "ssm_log_group_prefix" {
  type    = string
  default = "/findb/staging"
}

variable "findb_dns_check_name" {
  type    = string
  default = "findb-staging.tingfong.com"
}

variable "fetcher_dns_check_name" {
  type    = string
  default = "findb-staging.tingfong.com"
}

variable "minimum_free_disk_percent" {
  type    = number
  default = 15
}

variable "minimum_free_inode_percent" {
  type    = number
  default = 10
}

variable "tags" {
  description = "Additional non-sensitive tags applied to new control-plane resources."
  type        = map(string)
  default     = {}
}
