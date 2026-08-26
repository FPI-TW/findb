variable "aws_region" {
  description = "AWS region containing the existing staging EC2 instances."
  type        = string
  default     = "ap-southeast-1"

  validation {
    condition     = var.aws_region == "ap-southeast-1"
    error_message = "aws_region must remain the reviewed ap-southeast-1 staging region."
  }
}

variable "aws_account_id" {
  description = "Expected AWS account ID; apply fails if the provider is pointed at another account."
  type        = string
  default     = "439622209937"

  validation {
    condition     = var.aws_account_id == "439622209937"
    error_message = "aws_account_id must remain the reviewed staging account."
  }
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

  validation {
    condition     = var.github_repository == "FPI-TW/findb"
    error_message = "github_repository must remain the reviewed FPI-TW/findb repository."
  }
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

variable "infra_plan_role_name" {
  description = "Dedicated OIDC role used only by pull-request refresh plans."
  type        = string
  default     = "staging-infra-plan"

  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.infra_plan_role_name))
    error_message = "infra_plan_role_name must be a valid IAM role name."
  }
}

variable "state_bucket_name" {
  description = "Fixed private S3 bucket holding the staging control-plane state."
  type        = string
  default     = "findb-staging-tofu-state-439622209937"

  validation {
    condition     = var.state_bucket_name == "findb-staging-tofu-state-439622209937"
    error_message = "state_bucket_name must remain the reviewed staging state bucket."
  }
}

variable "state_key" {
  description = "Fixed state object key used by the staging control-plane backend."
  type        = string
  default     = "staging/control-plane.tfstate"

  validation {
    condition     = var.state_key == "staging/control-plane.tfstate"
    error_message = "state_key must remain the reviewed staging control-plane key."
  }
}

variable "state_kms_key_arn" {
  description = "Fixed customer-managed KMS key used to encrypt staging control-plane state."
  type        = string
  default     = "arn:aws:kms:ap-southeast-1:439622209937:key/776159fc-3251-4cd0-98b0-24dfa9e9701d"

  validation {
    condition     = var.state_kms_key_arn == "arn:aws:kms:ap-southeast-1:439622209937:key/776159fc-3251-4cd0-98b0-24dfa9e9701d"
    error_message = "state_kms_key_arn must remain the reviewed staging state key."
  }
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
    condition     = var.deploy_bundle_bucket_name == "findb-staging-deploy-bundle-439622209937"
    error_message = "deploy_bundle_bucket_name must remain the reviewed staging deployment-bundle bucket."
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
