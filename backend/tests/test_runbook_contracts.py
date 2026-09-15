"""Stable contracts for operator-facing runbook structure and commands."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _assert_headings(document: str, expected: tuple[str, ...]) -> None:
    headings = {line.strip() for line in document.splitlines() if line.startswith("#")}
    assert set(expected) <= headings


def _bash_blocks(document: str) -> tuple[str, ...]:
    return tuple(
        re.findall(
            r"^[ \t]*```bash[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
            document,
            flags=re.DOTALL | re.MULTILINE,
        )
    )


def test_tofu_runbook_documents_required_operator_workflows() -> None:
    runbook = _read("infra/tofu/README.md")

    _assert_headings(
        runbook,
        (
            "# Staging OpenTofu control plane",
            "## State bootstrap",
            "## Staging foundation",
            "## Pull-request plan gate",
            "## IAM boundaries",
            "## Phase 2 runtime-secret metadata and loader",
        ),
    )
    commands = "\n".join(_bash_blocks(runbook))
    for command in (
        "tofu -chdir=infra/tofu/bootstrap init -reconfigure",
        "tofu -chdir=infra/tofu/bootstrap init -migrate-state",
        "tofu -chdir=infra/tofu/staging import",
        "tofu -chdir=infra/tofu/staging plan -var-file=terraform.tfvars",
        "tofu -chdir=infra/tofu/staging apply -var-file=terraform.tfvars",
        "tofu -chdir=infra/tofu/staging output -json associate_instance_profile_commands",
    ):
        assert command in commands
    assert "aws ssm start-session" in commands
    assert "--document-name SSM-SessionManagerRunShell-findb-staging" in commands
    assert "--document-name SSM-SessionManagerRunShell-fetcher-staging" in commands


def test_monitoring_runbook_documents_safe_apply_and_notification_workflows() -> None:
    runbook = _read("docs/operations/monitoring.md")

    _assert_headings(
        runbook,
        (
            "# Staging Native Monitoring Runbook",
            "## Scope and current evidence boundary",
            "## Declared alarm contract",
            "## Applying monitoring changes",
            "## Controlled recipient replacement",
            "## Confirm and exercise notification delivery",
            "## Coverage gaps and cost / retention caveats",
        ),
    )
    commands = "\n".join(_bash_blocks(runbook))
    assert "-replace='aws_sns_topic_subscription.operational_alert_email'" in commands
    alarm_state_blocks = [
        block for block in _bash_blocks(runbook) if "aws cloudwatch set-alarm-state" in block
    ]
    assert any("--state-value ALARM" in block for block in alarm_state_blocks)
    assert any("--state-value OK" in block for block in alarm_state_blocks)
    assert "aws sns publish" not in commands


def test_ingestion_runbook_documents_secret_safe_queue_health_command() -> None:
    runbook = _read("docs/operations/ingestion.md")

    _assert_headings(
        runbook,
        (
            "# Durable Ingestion Runbook",
            "## 上線前檢查",
            "## Bounded end-to-end acceptance",
            "## Delivery completeness",
            "## 監控",
            "## RabbitMQ volume全毀",
            "## Pause與failure recovery",
        ),
    )
    required_tokens = (
        "/opt/findb/runtime-secrets/runtime_secret_command.sh",
        "--catalog /opt/findb/runtime-secrets/findb.json",
        "--consumer compose",
        "docker exec -e CELERY_BROKER_URL findb-ingest",
        "python /app/scripts/check_queue_health.py",
    )
    assert any(all(token in block for token in required_tokens) for block in _bash_blocks(runbook))
    assert "`export` `CELERY_BROKER_URL`" in runbook
