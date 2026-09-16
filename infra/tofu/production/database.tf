resource "aws_db_subnet_group" "production" {
  name       = "findb-production"
  subnet_ids = [aws_subnet.production["private_a"].id, aws_subnet.production["private_c"].id]
  tags = merge(local.common_tags, {
    DeploymentUnit = "database"
  })
}
resource "aws_kms_key" "rds" {
  description             = "FinDB production RDS"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags = merge(local.common_tags, {
    DeploymentUnit = "database"
  })
  lifecycle {
    prevent_destroy = true
  }
}
resource "aws_kms_alias" "rds" {
  name          = "alias/findb-production-rds"
  target_key_id = aws_kms_key.rds.key_id
}
resource "aws_db_instance" "production" {
  identifier                            = "findb-production"
  engine                                = "postgres"
  engine_version                        = "16"
  instance_class                        = "db.t4g.medium"
  allocated_storage                     = 100
  max_allocated_storage                 = 500
  storage_type                          = "gp3"
  storage_encrypted                     = true
  kms_key_id                            = aws_kms_key.rds.arn
  db_name                               = var.db_name
  username                              = var.db_master_username
  manage_master_user_password           = true
  db_subnet_group_name                  = aws_db_subnet_group.production.name
  vpc_security_group_ids                = [aws_security_group.database.id]
  availability_zone                     = "${var.aws_region}a"
  multi_az                              = false
  publicly_accessible                   = false
  backup_retention_period               = 14
  backup_window                         = "18:00-19:00"
  maintenance_window                    = "sun:19:00-sun:20:00"
  auto_minor_version_upgrade            = true
  deletion_protection                   = true
  skip_final_snapshot                   = false
  final_snapshot_identifier             = "findb-production-final"
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]
  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  copy_tags_to_snapshot                 = true
  tags = merge(local.common_tags, {
    DeploymentUnit = "database"
  })
  lifecycle {
    prevent_destroy = true
  }
}
