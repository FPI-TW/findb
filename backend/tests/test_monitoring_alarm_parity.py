"""Staging and production declare the same alarm rules, apart from reviewed differences."""

import re
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGING_ROOT = REPO_ROOT / "infra" / "tofu" / "staging"
PRODUCTION_ROOT = REPO_ROOT / "infra" / "tofu" / "production"

GTE = "GreaterThanOrEqualToThreshold"
LTE = "LessThanOrEqualToThreshold"
LT = "LessThanThreshold"


class Rule(NamedTuple):
    comparison: str
    threshold: float
    evaluation_periods: int
    datapoints_to_alarm: int
    treat_missing_data: str


# The one contract both stacks follow, keyed by (namespace family, metric).
# Adding, removing or retuning an alarm in one stack fails here until the other
# stack follows or the difference is reviewed into STAGING_DIFFERENCES.
CONTRACT: dict[tuple[str, str], Rule] = {
    ("AWS/EC2", "StatusCheckFailed"): Rule(GTE, 1, 2, 2, "missing"),
    ("AWS/EC2", "CPUUtilization"): Rule(GTE, 85, 3, 3, "missing"),
    ("AWS/RDS", "CPUUtilization"): Rule(GTE, 85, 2, 2, "missing"),
    ("AWS/RDS", "FreeStorageSpace"): Rule(LTE, 10 * 1024**3, 2, 2, "missing"),
    ("AWS/RDS", "DatabaseConnections"): Rule(GTE, 80, 2, 2, "missing"),
    ("AWS/RDS", "ReadLatency"): Rule(GTE, 0.1, 2, 2, "missing"),
    ("AWS/RDS", "WriteLatency"): Rule(GTE, 0.1, 2, 2, "missing"),
    # Sustained state: two periods, and a missing datum breaches.
    ("FinDB", "DiskUsedPercent"): Rule(GTE, 85, 2, 2, "breaching"),
    ("FinDB", "InodeUsedPercent"): Rule(GTE, 90, 2, 2, "breaching"),
    ("FinDB", "CollectorSuccess"): Rule(LT, 1, 2, 2, "breaching"),
    ("FinDB", "DockerContainerHealthy"): Rule(LT, 1, 2, 2, "breaching"),
    ("FinDB", "DockerRuntimeSecurityHealthy"): Rule(LT, 1, 2, 2, "breaching"),
    ("FinDB", "SchedulerHeartbeatAgeSeconds"): Rule(GTE, 180, 2, 2, "breaching"),
    ("FinDB", "FeedFreshnessHealthy"): Rule(LT, 1, 2, 2, "breaching"),
    ("FinDB", "TLSCertificateDaysRemaining"): Rule(LTE, 30, 2, 2, "breaching"),
    ("FinDB", "RDSBackupLagSeconds"): Rule(GTE, 1800, 2, 2, "breaching"),
    ("FinDB", "DLMPolicyHealthy"): Rule(LT, 1, 2, 2, "breaching"),
    ("FinDB", "LocalStateLargestFileBytes"): Rule(GTE, 2 * 1024**3, 2, 2, "breaching"),
    # Flags and integrity counts: one datum is enough, and missing still breaches.
    ("FinDB", "RabbitMQDiskAlarm"): Rule(GTE, 1, 1, 1, "breaching"),
    ("FinDB", "RabbitMQMemoryAlarm"): Rule(GTE, 1, 1, 1, "breaching"),
    ("FinDB", "LineageOrphanRows"): Rule(GTE, 1, 1, 1, "breaching"),
    ("FinDB", "EODDefaultPartitionRows"): Rule(GTE, 1, 1, 1, "breaching"),
    ("FinDB", "CredentialUsageAggregateMismatches"): Rule(GTE, 1, 1, 1, "breaching"),
    # Event counters: the collector alarm covers a missing datum.
    ("FinDB", "DockerRestartCount"): Rule(GTE, 3, 1, 1, "notBreaching"),
    ("FinDB", "InvalidCredentialEvents"): Rule(GTE, 1, 1, 1, "notBreaching"),
    ("FinDB", "ActiveFeedRejectedAttempts"): Rule(GTE, 1, 1, 1, "notBreaching"),
    ("FinDB", "ActiveFeedEmptySnapshots"): Rule(GTE, 1, 1, 1, "notBreaching"),
    ("FinDB", "ActiveFeedDQErrors"): Rule(GTE, 1, 1, 1, "notBreaching"),
    ("FinDB", "LocalStateBusyDiagnostics"): Rule(GTE, 1, 1, 1, "notBreaching"),
    ("FinDB", "LocalStateErrorDiagnostics"): Rule(GTE, 1, 1, 1, "notBreaching"),
    ("FinDB", "DeploymentFailure"): Rule(GTE, 1, 1, 1, "notBreaching"),
}

# Reviewed differences; every other field must match production.
STAGING_DIFFERENCES: dict[tuple[str, str], dict[str, object]] = {
    # Staging controls are normally stopped, and the collector publishes nothing
    # for a stopped control, so an unstarted scheduler must not alarm.
    ("FinDB", "SchedulerHeartbeatAgeSeconds"): {"treat_missing_data": "notBreaching"},
    ("FinDB", "FeedFreshnessHealthy"): {"treat_missing_data": "notBreaching"},
    # The staging database has 20 GiB allocated against 100 GiB in production.
    ("AWS/RDS", "FreeStorageSpace"): {"threshold": 5 * 1024**3},
}

_COMMON_VARIABLES = {
    "evaluation_periods": "monitoring_alarm_evaluation_periods",
    "datapoints_to_alarm": "monitoring_alarm_datapoints_to_alarm",
    "treat_missing_data": "monitoring_alarm_treat_missing_data",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _alarm_resources(monitoring: str) -> dict[str, str]:
    return dict(
        re.findall(
            r'(?ms)^resource "aws_cloudwatch_metric_alarm" "(\w+)" \{\n(.*?)^\}$', monitoring
        )
    )


def _attribute(block: str, name: str) -> str:
    match = re.search(rf"(?m)^\s*{name}\s+=\s+(.+)$", block)
    assert match is not None, f"{name} was not declared"
    return match.group(1)


def _staging_value(expression: str, variables: str) -> str:
    """Resolve a literal, a validated variable or the shared native-alarm local."""
    common = re.fullmatch(r"local\.monitoring_alarm_common\.(\w+)", expression)
    if common:
        expression = f"var.{_COMMON_VARIABLES[common.group(1)]}"
    variable = re.fullmatch(r"var\.(\w+)", expression)
    if variable:
        block = variables.split(f'variable "{variable.group(1)}"', 1)[1].split("variable ", 1)[0]
        expression = _attribute(block, "default")
    return expression.strip('"')


def _collect(rules: dict[tuple[str, str], set[Rule]], key: tuple[str, str], rule: Rule) -> None:
    rules.setdefault(key, set()).add(rule)


def _staging_rules() -> dict[tuple[str, str], set[Rule]]:
    monitoring = _read(STAGING_ROOT / "monitoring.tf")
    variables = _read(STAGING_ROOT / "variables.tf")
    resources = _alarm_resources(monitoring)
    rules: dict[tuple[str, str], set[Rule]] = {}

    def resolved(expression: str) -> str:
        return _staging_value(expression, variables)

    for name in ("ec2_status_check_failed", "ec2_cpu_utilization"):
        block = resources[name]
        _collect(
            rules,
            ("AWS/EC2", resolved(_attribute(block, "metric_name"))),
            Rule(
                resolved(_attribute(block, "comparison_operator")),
                float(resolved(_attribute(block, "threshold"))),
                int(resolved(_attribute(block, "evaluation_periods"))),
                int(resolved(_attribute(block, "datapoints_to_alarm"))),
                resolved(_attribute(block, "treat_missing_data")),
            ),
        )

    rds = resources["rds_native"]
    for metric, comparison, threshold in re.findall(
        r'metric_name\s+=\s+"(\w+)"\s+comparison_operator\s+=\s+"(\w+)"\s+threshold\s+=\s+(\S+)'
        r"\s+statistic",
        monitoring,
    ):
        _collect(
            rules,
            ("AWS/RDS", metric),
            Rule(
                comparison,
                float(resolved(threshold)),
                int(resolved(_attribute(rds, "evaluation_periods"))),
                int(resolved(_attribute(rds, "datapoints_to_alarm"))),
                resolved(_attribute(rds, "treat_missing_data")),
            ),
        )

    for metric, comparison, threshold, periods, datapoints, missing in re.findall(
        r'metric_name\s+=\s+"(\w+)"\s+comparison_operator\s+=\s+"(\w+)"\s+threshold\s+=\s+(\S+)'
        r'\s+unit\s+=\s+"\w+"\s+deployment_unit\s+=\s+\S+\s+resource\s+=\s+\S+'
        r"\s+evaluation_periods\s+=\s+(\d+)\s+datapoints_to_alarm\s+=\s+(\d+)"
        r'\s+treat_missing_data\s+=\s+"(\w+)"',
        monitoring,
    ):
        _collect(
            rules,
            ("FinDB", metric),
            Rule(comparison, float(resolved(threshold)), int(periods), int(datapoints), missing),
        )
    return rules


def _production_rules() -> dict[tuple[str, str], set[Rule]]:
    monitoring = _read(PRODUCTION_ROOT / "monitoring.tf")
    resources = _alarm_resources(monitoring)
    rules: dict[tuple[str, str], set[Rule]] = {}

    def literal(block: str, name: str) -> str:
        return _attribute(block, name).strip('"')

    for name, family in (
        ("ec2", "AWS/EC2"),
        ("ec2_cpu", "AWS/EC2"),
        ("deployment_failure", "FinDB"),
    ):
        block = resources[name]
        _collect(
            rules,
            (family, literal(block, "metric_name")),
            Rule(
                literal(block, "comparison_operator"),
                float(literal(block, "threshold")),
                int(literal(block, "evaluation_periods")),
                int(literal(block, "datapoints_to_alarm")),
                literal(block, "treat_missing_data"),
            ),
        )

    rds = resources["rds"]
    for metric, comparison, threshold in re.findall(
        r'metric = "(\w+)", op = "(\w+)", threshold = ([\d.]+), unit = "\w+"', rds
    ):
        _collect(
            rules,
            ("AWS/RDS", metric),
            Rule(
                comparison,
                float(threshold),
                int(literal(rds, "evaluation_periods")),
                int(literal(rds, "datapoints_to_alarm")),
                literal(rds, "treat_missing_data"),
            ),
        )

    for metric, comparison, threshold, periods, datapoints, missing in re.findall(
        r'metric = "(\w+)", resource = [^,]+, op = "(\w+)", threshold = ([\d.]+), '
        r'periods = (\d+), datapoints = (\d+), missing = "(\w+)" \}',
        monitoring,
    ):
        _collect(
            rules,
            ("FinDB", metric),
            Rule(comparison, float(threshold), int(periods), int(datapoints), missing),
        )
    return rules


def _expected(
    differences: dict[tuple[str, str], dict[str, object]],
) -> dict[tuple[str, str], set[Rule]]:
    return {key: {rule._replace(**differences.get(key, {}))} for key, rule in CONTRACT.items()}


def test_both_environments_follow_the_alarm_contract() -> None:
    assert set(STAGING_DIFFERENCES) <= set(CONTRACT)

    assert _production_rules() == _expected({})
    assert _staging_rules() == _expected(STAGING_DIFFERENCES)


def test_production_custom_alarms_read_each_declared_rule() -> None:
    custom = _alarm_resources(_read(PRODUCTION_ROOT / "monitoring.tf"))["custom"]

    # Without these the per-entry rules above would be declared but unused.
    assert _attribute(custom, "evaluation_periods") == "each.value.periods"
    assert _attribute(custom, "datapoints_to_alarm") == "each.value.datapoints"
    assert _attribute(custom, "comparison_operator") == "each.value.op"
    assert _attribute(custom, "threshold") == "each.value.threshold"
    assert _attribute(custom, "treat_missing_data").endswith(": each.value.missing")


def test_every_alarm_notifies_on_recovery_in_both_environments() -> None:
    for root, topic in (
        (STAGING_ROOT, "aws_sns_topic.operational_alerts.arn"),
        (PRODUCTION_ROOT, "aws_sns_topic.operations.arn"),
    ):
        resources = _alarm_resources(_read(root / "monitoring.tf"))
        assert resources
        for name, block in resources.items():
            # Only a retired identity may keep an empty recovery action list.
            assert _attribute(block, "ok_actions") in {
                f"[{topic}]",
                f"contains(local.retired_alarm_resources, each.value.resource) ? [] : [{topic}]",
            }, f"{root.name} {name} does not notify on recovery"


def test_staging_alarms_do_not_notify_while_production_alarms_do() -> None:
    staging = _read(STAGING_ROOT / "monitoring.tf")
    # Staging runs one provider at a time for tests; by Owner decision no staging
    # alarm notifies, through one switch every alarm resource reads.
    assert re.search(r"(?m)^\s*alarm_notifications_enabled = false$", staging)
    for name, block in _alarm_resources(staging).items():
        assert _attribute(block, "actions_enabled").startswith(
            "local.alarm_notifications_enabled"
        ), f"staging {name} does not read the notification switch"

    production = _read(PRODUCTION_ROOT / "monitoring.tf")
    assert "alarm_notifications_enabled" not in production
    for name, block in _alarm_resources(production).items():
        # An undeclared actions_enabled is the provider default, true.
        declared = re.search(r"(?m)^\s*actions_enabled\s+=\s+(.+)$", block)
        assert declared is None or declared.group(1) in {
            "true",
            "!contains(local.retired_alarm_resources, each.value.resource)",
        }, f"production {name} does not notify"
