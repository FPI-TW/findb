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

variable "findb_rds_instance_identifier" {
  description = "Existing FinDB staging RDS DB instance identifier monitored by native CloudWatch metrics; it is referenced, not managed."
  type        = string
  default     = "fin-db"

  validation {
    condition     = var.findb_rds_instance_identifier == "fin-db"
    error_message = "findb_rds_instance_identifier must remain the reviewed fin-db staging RDS instance."
  }
}

variable "operational_alert_email" {
  description = "Required confirmed email recipient for private staging operational-alert notifications. This non-secret contact value is supplied outside version control; subscription endpoint changes require the reviewed replacement procedure because normal drift is ignored."
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.operational_alert_email))
    error_message = "operational_alert_email must be a valid email address."
  }
}

variable "monitoring_alarm_period_seconds" {
  description = "CloudWatch period used by every Phase 6 native alarm."
  type        = number
  default     = 300

  validation {
    condition     = var.monitoring_alarm_period_seconds == 300
    error_message = "monitoring_alarm_period_seconds must remain the reviewed five-minute period."
  }
}

variable "monitoring_alarm_evaluation_periods" {
  description = "Consecutive five-minute periods evaluated by every Phase 6 native alarm."
  type        = number
  default     = 2

  validation {
    condition     = var.monitoring_alarm_evaluation_periods == 2
    error_message = "monitoring_alarm_evaluation_periods must remain the reviewed two periods."
  }
}

variable "monitoring_alarm_datapoints_to_alarm" {
  description = "Breaching datapoints required within the evaluation window for every Phase 6 native alarm."
  type        = number
  default     = 2

  validation {
    condition     = var.monitoring_alarm_datapoints_to_alarm == 2
    error_message = "monitoring_alarm_datapoints_to_alarm must remain the reviewed two datapoints."
  }
}

variable "monitoring_alarm_treat_missing_data" {
  description = "Missing-data policy for every Phase 6 native alarm; missing must remain visible as INSUFFICIENT_DATA."
  type        = string
  default     = "missing"

  validation {
    condition     = var.monitoring_alarm_treat_missing_data == "missing"
    error_message = "monitoring_alarm_treat_missing_data must remain missing."
  }
}

variable "monitoring_ec2_status_check_failed_threshold" {
  description = "EC2 StatusCheckFailed maximum value that opens an alarm."
  type        = number
  default     = 1

  validation {
    condition     = var.monitoring_ec2_status_check_failed_threshold == 1
    error_message = "monitoring_ec2_status_check_failed_threshold must remain one failed status check."
  }
}

variable "monitoring_rds_free_storage_space_threshold_bytes" {
  description = "RDS FreeStorageSpace minimum safe capacity in bytes."
  type        = number
  default     = 5368709120

  validation {
    condition     = var.monitoring_rds_free_storage_space_threshold_bytes == 5368709120
    error_message = "monitoring_rds_free_storage_space_threshold_bytes must remain the reviewed 5 GiB threshold."
  }
}

variable "monitoring_rds_database_connections_threshold" {
  description = "RDS DatabaseConnections average value that opens an alarm."
  type        = number
  default     = 80

  validation {
    condition     = var.monitoring_rds_database_connections_threshold == 80
    error_message = "monitoring_rds_database_connections_threshold must remain the reviewed 80-connection threshold."
  }
}

variable "monitoring_rds_latency_threshold_seconds" {
  description = "RDS ReadLatency and WriteLatency average value in seconds that opens an alarm."
  type        = number
  default     = 0.1

  validation {
    condition     = var.monitoring_rds_latency_threshold_seconds == 0.1
    error_message = "monitoring_rds_latency_threshold_seconds must remain the reviewed 100 ms threshold."
  }
}

variable "monitoring_disk_used_percent_threshold" {
  description = "Root filesystem used-percent threshold for both staging hosts."
  type        = number
  default     = 85

  validation {
    condition     = var.monitoring_disk_used_percent_threshold == 85
    error_message = "monitoring_disk_used_percent_threshold must remain the reviewed 85 percent threshold."
  }
}

variable "monitoring_inode_used_percent_threshold" {
  description = "Root filesystem inode used-percent threshold for both staging hosts."
  type        = number
  default     = 90

  validation {
    condition     = var.monitoring_inode_used_percent_threshold == 90
    error_message = "monitoring_inode_used_percent_threshold must remain the reviewed 90 percent threshold."
  }
}

variable "monitoring_docker_restart_count_threshold" {
  description = "Current-container restart count that opens a Docker restart alarm."
  type        = number
  default     = 3

  validation {
    condition     = var.monitoring_docker_restart_count_threshold == 3
    error_message = "monitoring_docker_restart_count_threshold must remain three restarts."
  }
}

variable "monitoring_scheduler_heartbeat_age_threshold_seconds" {
  description = "Maximum accepted age for each active Fetcher scheduler heartbeat."
  type        = number
  default     = 180

  validation {
    condition     = var.monitoring_scheduler_heartbeat_age_threshold_seconds == 180
    error_message = "monitoring_scheduler_heartbeat_age_threshold_seconds must remain three minutes."
  }
}

variable "monitoring_rds_backup_lag_threshold_seconds" {
  description = "Maximum accepted lag between now and the RDS latest restorable time."
  type        = number
  default     = 1800

  validation {
    condition     = var.monitoring_rds_backup_lag_threshold_seconds == 1800
    error_message = "monitoring_rds_backup_lag_threshold_seconds must remain 30 minutes."
  }
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

variable "ecr_repository_names" {
  description = "Fixed private staging ECR repository names, split by deployment unit."
  type        = map(string)
  default = {
    findb_backend       = "findb/staging/backend"
    findb_dashboard     = "findb/staging/dashboard"
    fetcher_twelve_data = "findb/staging/fetcher/twelve-data"
    fetcher_finlab      = "findb/staging/fetcher/finlab"
    fetcher_shioaji     = "findb/staging/fetcher/shioaji"
  }
  validation {
    condition = length(var.ecr_repository_names) == 5 && alltrue([
      try(var.ecr_repository_names["findb_backend"], null) == "findb/staging/backend",
      try(var.ecr_repository_names["findb_dashboard"], null) == "findb/staging/dashboard",
      try(var.ecr_repository_names["fetcher_twelve_data"], null) == "findb/staging/fetcher/twelve-data",
      try(var.ecr_repository_names["fetcher_finlab"], null) == "findb/staging/fetcher/finlab",
      try(var.ecr_repository_names["fetcher_shioaji"], null) == "findb/staging/fetcher/shioaji",
    ])
    error_message = "ecr_repository_names must remain the five reviewed staging ECR repository names."
  }
}

variable "ecr_untagged_image_retention_days" {
  type    = number
  default = 7
  validation {
    condition     = var.ecr_untagged_image_retention_days == 7
    error_message = "ecr_untagged_image_retention_days must remain the reviewed seven-day retention period."
  }
}

variable "findb_ecr_publisher_role_name" {
  type    = string
  default = "findb-staging-ecr-publisher"
  validation {
    condition     = var.findb_ecr_publisher_role_name == "findb-staging-ecr-publisher"
    error_message = "findb_ecr_publisher_role_name must remain findb-staging-ecr-publisher."
  }
}

variable "fetcher_ecr_publisher_role_name" {
  type    = string
  default = "fetcher-staging-ecr-publisher"
  validation {
    condition     = var.fetcher_ecr_publisher_role_name == "fetcher-staging-ecr-publisher"
    error_message = "fetcher_ecr_publisher_role_name must remain fetcher-staging-ecr-publisher."
  }
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
