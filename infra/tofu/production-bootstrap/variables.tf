variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
  validation {
    condition     = var.aws_region == "ap-southeast-1"
    error_message = "Production is pinned to ap-southeast-1."
  }
}
variable "aws_account_id" {
  type    = string
  default = "289112218471"
  validation {
    condition     = var.aws_account_id == "289112218471"
    error_message = "Production account mismatch."
  }
}
variable "state_bucket_name" {
  type    = string
  default = "findb-production-tofu-state-289112218471"
  validation {
    condition     = var.state_bucket_name == "findb-production-tofu-state-289112218471"
    error_message = "State bucket name is fixed."
  }
}
variable "state_kms_alias" {
  type    = string
  default = "alias/findb-production-tofu-state"
}
variable "tags" {
  type = map(string)
  default = {
    Project = "findb", Environment = "production", DeploymentUnit = "control-plane",
    Owner   = "tylercore", BackupOwner = "tylercore"

  }
}
