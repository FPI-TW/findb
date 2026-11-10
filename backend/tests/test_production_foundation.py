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
        for job in ("verify", "promote", "deploy-promotion", "deploy-rollback"):
            assert f"    - {job}\n" in needs
        assert "    - release\n" not in needs
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


def test_production_alert_topic_key_lets_only_own_alarms_publish() -> None:
    storage = (REPO_ROOT / "infra/tofu/production/storage.tf").read_text(encoding="utf-8")
    monitoring = (REPO_ROOT / "infra/tofu/production/monitoring.tf").read_text(encoding="utf-8")

    # The alert topic is encrypted with the logs key; without a CloudWatch grant
    # every alarm action fails to publish.
    assert "kms_master_key_id = aws_kms_key.logs.id" in monitoring
    logs_key = storage.split('resource "aws_kms_key" "logs"', 1)[1].split("\n}\n", 1)[0]
    assert "policy                  = data.aws_iam_policy_document.logs_kms.json" in logs_key
    grant = storage.split('data "aws_iam_policy_document" "logs_kms"', 1)[1].split(
        'resource "aws_kms_key" "logs"', 1
    )[0]
    assert "source_policy_documents = [data.aws_iam_policy_document.kms.json]" in grant
    assert 'identifiers = ["cloudwatch.amazonaws.com"]' in grant
    assert 'actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]' in grant
    assert 'variable = "aws:SourceAccount"' in grant
    assert ":alarm:findb-production-*" in grant
    # The shared policy for the deploy bundle key stays unchanged.
    shared = storage.split('data "aws_iam_policy_document" "kms"', 1)[1].split(
        'resource "aws_kms_key" "deploy_bundle"', 1
    )[0]
    assert "cloudwatch.amazonaws.com" not in shared


def test_production_backend_pins_the_state_kms_key_required_by_the_bucket_policy() -> None:
    backend = (REPO_ROOT / "infra/tofu/production/backend.tf").read_text(encoding="utf-8")
    bootstrap = (REPO_ROOT / "infra/tofu/production-bootstrap/main.tf").read_text(encoding="utf-8")

    # The bucket policy denies any PutObject (state and lockfile) whose SSE-KMS
    # key id is not the state key, so a plain `init` must already carry it.
    assert 'variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"' in bootstrap
    assert 'bucket       = "findb-production-tofu-state-289112218471"' in backend
    assert 'key          = "production/control-plane.tfstate"' in backend
    assert "encrypt      = true" in backend
    assert (
        'kms_key_id   = "arn:aws:kms:ap-southeast-1:289112218471:key/'
        'be629e7b-7540-4f6f-9818-a0b8f2d7bddc"'
    ) in backend
    assert "use_lockfile = true" in backend
    assert "dynamodb_table" not in backend


def test_production_rollback_replays_acceptance_without_rerunning_ci() -> None:
    for unit in ("findb", "fetcher"):
        workflow = (REPO_ROOT / ".github/workflows" / f"{unit}-production-cd.yml").read_text(
            encoding="utf-8"
        )
        verify = workflow.split("  verify:", 1)[1].split("  promote:", 1)[0]
        assert "if: ${{ inputs.mode == 'promote' }}" in verify
        rollback = workflow.split("  deploy-rollback:", 1)[1].split(
            "  report-production-deployment-failure:", 1
        )[0]
        assert "    - verify\n" not in rollback
        assert "needs.release.result == 'success'" in rollback
        assert "mode: replay" in rollback
        promote = workflow.split("  promote:", 1)[1].split("  deploy-promotion:", 1)[0]
        assert "needs.verify.result == 'success'" in promote


def test_both_fetcher_hosts_keep_one_four_gib_swap_file() -> None:
    script = (REPO_ROOT / "infra/deploy/ensure_swap.sh").read_text(encoding="utf-8")
    assert "size_bytes=4294967296" in script
    assert "vm.swappiness = 10" in script
    for environment, path, target in (
        ("production", "infra/tofu/production/compute.tf", 'aws_instance.unit["fetcher"].id'),
        ("staging", "infra/tofu/staging/main.tf", "local.unit_config.fetcher.instance_id"),
    ):
        iac = (REPO_ROOT / path).read_text(encoding="utf-8")
        association = iac.split('resource "aws_ssm_association" "fetcher_swap"', 1)[1]
        assert f'"findb-{environment}-fetcher-swap"' in association
        assert "deploy/ensure_swap.sh" in association
        assert f"values = [{target}]" in association
