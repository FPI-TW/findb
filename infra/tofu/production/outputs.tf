output "account_id" {
  value = data.aws_caller_identity.current.account_id
}
output "vpc_id" {
  value = aws_vpc.production.id
}
output "instance_ids" {
  value = {
    for unit, instance in aws_instance.unit : unit => instance.id
  }
}
output "elastic_ips" {
  value = {
    for unit, address in aws_eip.unit : unit => address.public_ip
  }
}
output "source_allowlist_cidrs" {
  value = "${aws_eip.unit["fetcher"].public_ip}/32"
}
output "rds_endpoint" {
  value = aws_db_instance.production.address
}
output "ecr_repository_urls" {
  value = {
    for key, repo in aws_ecr_repository.production : key => repo.repository_url
  }
}
output "deploy_role_arns" {
  value = {
    for unit, role in aws_iam_role.deploy : unit => role.arn
  }
}
output "promotion_role_arns" {
  value = {
    for unit, role in aws_iam_role.promotion : unit => role.arn
  }
}
output "instance_role_arns" {
  value = {
    for unit, role in aws_iam_role.instance : unit => role.arn
  }
}
output "deploy_bundle_bucket" {
  value = aws_s3_bucket.deploy_bundle.id
}
output "deploy_bundle_kms_key_arn" {
  value = aws_kms_key.deploy_bundle.arn
}
output "runtime_secret_names" {
  value = {
    for key, secret in aws_secretsmanager_secret.runtime : key => secret.name
  }
}
output "ssm_log_groups" {
  value = {
    for unit, group in aws_cloudwatch_log_group.ssm : unit => group.name
  }
}
output "github_environment_variables" {
  value = {
    AWS_ACCOUNT_ID            = var.aws_account_id
    AWS_REGION                = var.aws_region
    ECR_REGISTRY              = "${var.aws_account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
    DEPLOY_BUNDLE_BUCKET      = aws_s3_bucket.deploy_bundle.id
    DEPLOY_BUNDLE_KMS_KEY_ARN = aws_kms_key.deploy_bundle.arn
    FINDB_PUBLIC_HOST         = "findb.tingfong.com"
    SOURCE_ALLOWLIST_CIDRS    = "${aws_eip.unit["fetcher"].public_ip}/32"

  }
}
