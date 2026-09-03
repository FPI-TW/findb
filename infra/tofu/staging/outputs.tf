output "github_oidc_provider_arn" {
  description = "GitHub OIDC provider used by both environment-bound deploy roles."
  value       = local.github_oidc_provider_arn
}

output "infra_plan_role_arn" {
  description = "Dedicated pull-request OpenTofu refresh-plan role ARN."
  value       = aws_iam_role.infra_plan.arn
}

output "operational_alert_topic_arn" {
  description = "Private KMS-encrypted SNS topic for Phase 6 native and bounded custom operational alarms. Confirm the configured email subscription before relying on delivery."
  value       = aws_sns_topic.operational_alerts.arn
}

output "metric_publisher_association_ids" {
  description = "Unit-specific five-minute SSM associations that publish bounded FinDB/Staging operational metrics."
  value       = { for unit, association in aws_ssm_association.staging_metric_publisher : unit => association.association_id }
}

output "deploy_role_arns" {
  description = "Separate OIDC deploy role ARN for each staging environment."
  value = {
    for unit, role in aws_iam_role.deploy : unit => role.arn
  }
}

output "ecr_publisher_role_arns" {
  description = "Separate main-only GitHub OIDC publisher role ARN for each staging ECR unit."
  value       = { for unit, role in aws_iam_role.ecr_publisher : unit => role.arn }
}

output "ecr_repository_names" {
  description = "Fixed private staging ECR repository names keyed by image role."
  value       = { for key, repository in aws_ecr_repository.staging : key => repository.name }
}

output "ecr_repository_urls" {
  description = "Private staging ECR repository URLs keyed by image role."
  value       = { for key, repository in aws_ecr_repository.staging : key => repository.repository_url }
}

output "ecr_repository_arns" {
  description = "Private staging ECR repository ARNs keyed by image role."
  value       = { for key, repository in aws_ecr_repository.staging : key => repository.arn }
}

output "instance_role_arns" {
  description = "Separate EC2 instance role ARN for each staging unit."
  value = {
    for unit, role in aws_iam_role.instance : unit => role.arn
  }
}

output "runtime_secret_kms_key_arns" {
  description = "Separate customer-managed KMS keys for each staging runtime-secret boundary."
  value = {
    for unit, key in aws_kms_key.runtime_secrets : unit => key.arn
  }
}

output "runtime_secret_names" {
  description = "Metadata-only Secrets Manager names. Secret values are populated outside OpenTofu state."
  value = {
    for key, secret in aws_secretsmanager_secret.active_runtime : key => secret.name
  }
}

output "instance_profile_names" {
  description = "Instance profile names to associate with the existing EC2 targets."
  value = {
    for unit, profile in aws_iam_instance_profile.instance : unit => profile.name
  }
}

output "ssm_log_group_names" {
  description = "Unit-specific CloudWatch log groups used by Run Command and Session Manager."
  value = {
    for unit, group in aws_cloudwatch_log_group.ssm : unit => group.name
  }
}

output "session_manager_document_names" {
  description = "Custom Session Manager preference document name for each staging unit."
  value = {
    for unit, document in aws_ssm_document.session_manager_preferences : unit => document.name
  }
}

output "deploy_bundle_bucket_name" {
  description = "Private versioned KMS-encrypted deployment bundle bucket."
  value       = aws_s3_bucket.deploy_bundle.bucket
}

output "deploy_bundle_kms_key_arn" {
  description = "KMS key used by the deployment bundle bucket and SSM logs."
  value       = aws_kms_key.deploy_bundle.arn
}

output "existing_instance_ids" {
  description = "The existing target instances referenced by this stack."
  value = {
    findb   = data.aws_instance.findb.id
    fetcher = data.aws_instance.fetcher.id
  }
}

output "associate_instance_profile_commands" {
  description = "One-time operator commands; run only after reviewing the plan and confirming the target IDs."
  value = {
    for unit, config in local.unit_config : unit => "aws ec2 associate-iam-instance-profile --region ${var.aws_region} --instance-id ${config.instance_id} --iam-instance-profile Name=${aws_iam_instance_profile.instance[unit].name}"
  }
}
