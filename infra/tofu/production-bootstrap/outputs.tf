output "state_bucket_name" {
  value = aws_s3_bucket.state.id
}
output "state_kms_key_arn" {
  value = aws_kms_key.state.arn
}
output "backend_init_command" {
  # production/backend.tf pins every backend value, including this state key;
  # passing -backend-config here would let the two copies drift.
  value = "tofu -chdir=infra/tofu/production init -reconfigure"
}
output "bootstrap_backend_migrate_command" {
  value = "tofu -chdir=infra/tofu/production-bootstrap init -migrate-state -backend-config=bucket=${aws_s3_bucket.state.id} -backend-config=key=production/bootstrap.tfstate -backend-config=region=${var.aws_region} -backend-config=encrypt=true -backend-config=kms_key_id=${aws_kms_key.state.arn} -backend-config=use_lockfile=true"
}
