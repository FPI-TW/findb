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
