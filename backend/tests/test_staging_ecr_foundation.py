"""Focused structural checks for the bounded staging ECR foundation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "infra" / "tofu" / "staging"
ECR_BUILD_HELPER = ROOT / "infra" / "deploy" / "runtime-secrets" / "build_ecr_image_if_missing.sh"


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
    deploy = iam.split('data "aws_iam_policy_document" "deploy_permissions"', 1)[1].split(
        'resource "aws_iam_role_policy" "deploy_permissions"', 1
    )[0]
    assert "ecr:" not in deploy


def test_ecr_immutable_sha_build_helper_reuses_only_exact_main_sha_tags() -> None:
    helper = ECR_BUILD_HELPER.read_text(encoding="utf-8")
    assert (
        'readonly expected_registry="439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"' in helper
    )
    for repository in (
        "findb/staging/backend",
        "findb/staging/dashboard",
        "findb/staging/fetcher/twelve-data",
        "findb/staging/fetcher/finlab",
        "findb/staging/fetcher/shioaji",
    ):
        assert f'"$expected_registry/{repository}"' in helper
    assert '[ "$GITHUB_REF" != "refs/heads/main" ]' in helper
    assert "'^[0-9a-f]{40}$'" in helper
    assert "aws ecr describe-images" in helper
    assert '"imageTag=$image_tag"' in helper
    assert "ImageNotFoundException" in helper
    assert "ecr_tag_inspection_failed" in helper
    assert "rollback_tag_not_found" in helper
    assert "ECR_REUSE_ONLY:-false" in helper
    assert "docker buildx build" in helper
    assert helper.index("docker buildx build") > helper.index("ImageNotFoundException")
