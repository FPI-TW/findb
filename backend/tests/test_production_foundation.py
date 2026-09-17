"""Structural regression checks for the Production AWS foundation."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_production_hosts_use_explicit_eips_without_provider_replacement_drift() -> None:
    compute = (REPO_ROOT / "infra/tofu/production/compute.tf").read_text(encoding="utf-8")
    network = (REPO_ROOT / "infra/tofu/production/network.tf").read_text(encoding="utf-8")

    # Once an EIP is attached, the AWS provider reports the instance as having
    # an associated public address. Pinning this argument to false therefore
    # creates a perpetual ForceNew diff even though the subnet itself does not
    # auto-assign public addresses.
    assert "associate_public_ip_address" not in compute
    assert "depends_on             = [aws_iam_role_policy.instance_base]" in compute
    assert 'resource "aws_eip" "unit"' in compute
    assert "instance = aws_instance.unit[each.key].id" in compute
    assert "map_public_ip_on_launch = false" in network


def test_production_hosts_install_the_compose_v2_runtime_dependency() -> None:
    compute = (REPO_ROOT / "infra/tofu/production/compute.tf").read_text(encoding="utf-8")

    assert 'host_packages = "docker.io docker-compose-v2 jq python3"' in compute
    assert "apt-get install -y ${local.host_packages}" in compute
    assert 'resource "aws_ssm_association" "host_dependencies"' in compute
    assert '"docker compose version >/dev/null"' in compute


def test_only_findb_production_deployer_can_resolve_the_rds_endpoint() -> None:
    iam = (REPO_ROOT / "infra/tofu/production/iam.tf").read_text(encoding="utf-8")
    deploy_policy = iam.split('data "aws_iam_policy_document" "deploy"', 1)[1].split(
        'resource "aws_iam_role_policy" "deploy"', 1
    )[0]
    rds_statement = deploy_policy.split("# Only the FinDB deploy workflow", 1)[1].split(
        'sid       = "SendDocument"', 1
    )[0]

    assert 'for_each = each.key == "findb" ? [true] : []' in rds_statement
    assert 'sid       = "ReadFinDBRdsEndpoint"' in rds_statement
    assert 'actions   = ["rds:DescribeDBInstances"]' in rds_statement
    assert 'resources = ["*"]' in rds_statement
    assert deploy_policy.count('"rds:DescribeDBInstances"') == 1
