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


def test_production_cd_reports_deployment_failures_with_unit_scoped_metrics() -> None:
    for unit in ("findb", "fetcher"):
        workflow = (REPO_ROOT / ".github/workflows" / f"{unit}-production-cd.yml").read_text(
            encoding="utf-8"
        )
        report_job = workflow.split("  report-production-deployment-failure:", 1)[1]
        needs = report_job.split("    if:", 1)[0]
        for job in ("bind-release-tag", "verify", "promote", "deploy-promotion", "deploy-rollback"):
            assert f"    - {job}\n" in needs
        assert "    - resolve\n" not in needs
        assert "always() && github.ref == 'refs/heads/main'" in report_job
        assert "contains(needs.*.result, 'failure')" in report_job
        assert "contains(needs.*.result, 'cancelled')" in report_job
        assert "id-token: write" in report_job
        assert "target: production" in report_job
        assert "--namespace FinDB/Production" in report_job
        assert "--metric-name DeploymentFailure" in report_job
        assert f"--dimensions DeploymentUnit={unit},Resource=github-actions" in report_job


def test_production_deployment_failure_alarm_fires_on_one_published_failure() -> None:
    monitoring = (REPO_ROOT / "infra/tofu/production/monitoring.tf").read_text(encoding="utf-8")
    custom = monitoring.split("custom_alarms = merge(", 1)[1].split("metric_service", 1)[0]
    assert "DeploymentFailure" not in custom
    alarm = monitoring.split('resource "aws_cloudwatch_metric_alarm" "deployment_failure"', 1)[
        1
    ].split("\n}\n", 1)[0]
    assert "for_each            = local.units" in alarm
    assert 'namespace           = "FinDB/Production"' in alarm
    assert "evaluation_periods  = 1" in alarm
    assert "datapoints_to_alarm = 1" in alarm
    assert 'treat_missing_data  = "notBreaching"' in alarm
    assert 'DeploymentUnit = each.key, Resource = "github-actions"' in alarm

    iam = (REPO_ROOT / "infra/tofu/production/iam.tf").read_text(encoding="utf-8")
    metric_statement = iam.split('sid       = "DeploymentFailureMetric"', 1)[1].split(
        "statement {", 1
    )[0]
    assert 'actions   = ["cloudwatch:PutMetricData"]' in metric_statement
    assert 'values   = ["FinDB/Production"]' in metric_statement
