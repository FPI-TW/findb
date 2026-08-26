output "state_bucket_name" {
  description = "Private versioned bucket used by the staging OpenTofu backend."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_arn" {
  description = "ARN of the staging OpenTofu backend bucket."
  value       = aws_s3_bucket.state.arn
}

output "state_kms_key_arn" {
  description = "KMS key ARN used for OpenTofu state encryption."
  value       = aws_kms_key.state.arn
}

output "bootstrap_backend_migrate_command" {
  description = "Migrate the short-lived local bootstrap state to its separate native-lockfile S3 key."
  value       = "tofu -chdir=infra/tofu/bootstrap init -migrate-state -backend-config=bucket=${aws_s3_bucket.state.bucket} -backend-config=key=staging/bootstrap.tfstate -backend-config=region=${var.aws_region} -backend-config=encrypt=true -backend-config=kms_key_id=${aws_kms_key.state.arn} -backend-config=use_lockfile=true"
}

output "backend_init_command" {
  description = "Native S3 lockfile backend initialization command for the staging foundation."
  value       = "tofu -chdir=infra/tofu/staging init -backend-config=bucket=${aws_s3_bucket.state.bucket} -backend-config=key=staging/control-plane.tfstate -backend-config=region=${var.aws_region} -backend-config=encrypt=true -backend-config=kms_key_id=${aws_kms_key.state.arn} -backend-config=use_lockfile=true"
}
