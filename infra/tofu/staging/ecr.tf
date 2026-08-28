resource "aws_ecr_repository" "staging" {
  for_each = local.ecr_repository_config

  name                 = each.value.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  encryption_configuration { encryption_type = "AES256" }
  image_scanning_configuration { scan_on_push = true }
  tags = merge(local.common_tags, { DeploymentUnit = each.value.unit })

  lifecycle { prevent_destroy = true }
}

resource "aws_ecr_lifecycle_policy" "untagged_images" {
  for_each   = local.ecr_repository_config
  repository = aws_ecr_repository.staging[each.key].name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images older than ${var.ecr_untagged_image_retention_days} days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = var.ecr_untagged_image_retention_days
      }
      action = { type = "expire" }
    }]
  })
  lifecycle { prevent_destroy = true }
}
