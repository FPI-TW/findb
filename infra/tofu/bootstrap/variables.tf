variable "aws_region" {
  description = "AWS region for the OpenTofu state bootstrap."
  type        = string
  default     = "ap-southeast-1"
}

variable "aws_account_id" {
  description = "Expected AWS account ID; provider configuration refuses another account."
  type        = string
  default     = "439622209937"
}

variable "state_bucket_name" {
  description = "Globally unique private S3 bucket name for OpenTofu state."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid S3 bucket name."
  }
}

variable "state_kms_alias" {
  description = "Alias for the KMS key that encrypts the state bucket."
  type        = string
  default     = "alias/findb-staging-tofu-state"
}

variable "tags" {
  description = "Non-sensitive ownership tags applied to bootstrap resources."
  type        = map(string)
  default = {
    Project        = "findb"
    Environment    = "staging"
    DeploymentUnit = "control-plane"
    Owner          = "tylercore"
    BackupOwner    = "tylercore"
  }

  validation {
    condition = alltrue([
      for key in ["Project", "Environment", "DeploymentUnit", "Owner", "BackupOwner"] : contains(keys(var.tags), key)
    ]) && try(var.tags["Project"], "") == "findb"
    error_message = "Bootstrap tags must include the required contract and Project must be lowercase findb."
  }
}
