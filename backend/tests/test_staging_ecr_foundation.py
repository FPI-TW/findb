"""Focused structural checks for the bounded staging ECR foundation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "infra" / "tofu" / "staging"


def test_ecr_foundation_is_fixed_immutable_and_retains_only_untagged_images() -> None:
    ecr = (STAGING / "ecr.tf").read_text(encoding="utf-8")
    variables = (STAGING / "variables.tf").read_text(encoding="utf-8")
    for name in (
        "findb/staging/backend",
        "findb/staging/dashboard",
        "findb/staging/fetcher/twelve-data",
        "findb/staging/fetcher/finlab",
        "findb/staging/fetcher/shioaji",
    ):
        assert name in variables
    for rule in (
        'image_tag_mutability = "IMMUTABLE"',
        "force_delete         = false",
        'encryption_type = "AES256"',
        "scan_on_push = true",
        "prevent_destroy = true",
        'tagStatus   = "untagged"',
        'countType   = "sinceImagePushed"',
    ):
        assert rule in ecr
    assert 'tagStatus = "tagged"' not in ecr
    assert "default = 7" in variables


def test_ecr_iam_separates_main_publishers_instance_pulls_and_deploy_roles() -> None:
    iam = (STAGING / "iam.tf").read_text(encoding="utf-8")
    assert '"repo:${var.github_repository}:ref:refs/heads/main"' in iam
    assert "findb-staging-ecr-publisher" in (STAGING / "variables.tf").read_text(encoding="utf-8")
    assert "fetcher-staging-ecr-publisher" in (STAGING / "variables.tf").read_text(encoding="utf-8")
    assert "PushAndInspectOwnEcrImages" in iam
    assert "PullAndInspectOwnEcrImages" in iam
    publisher = iam.split('data "aws_iam_policy_document" "ecr_publisher_permissions"', 1)[1].split(
        'resource "aws_iam_role_policy" "ecr_publisher_permissions"', 1
    )[0]
    assert 'for_each = each.key == "findb" ? [true] : []' in publisher
    assert 'sid       = "PullBackendForMigrationInspection"' in publisher
    assert 'actions   = ["ecr:GetDownloadUrlForLayer"]' in publisher
    assert 'resources = [aws_ecr_repository.staging["findb_backend"].arn]' in publisher
    deploy = iam.split('data "aws_iam_policy_document" "deploy_permissions"', 1)[1].split(
        'resource "aws_iam_role_policy" "deploy_permissions"', 1
    )[0]
    assert "ecr:" not in deploy
