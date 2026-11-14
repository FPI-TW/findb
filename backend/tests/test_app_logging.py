"""API roles emit app INFO records; the lifespan leaves logger state as it found it."""

import logging
import re

from app.main import app_logging


def test_app_logging_emits_utc_info_and_restores_logger(capsys, caplog):
    logger = logging.getLogger("app")
    handlers, level = list(logger.handlers), logger.level
    with caplog.at_level(logging.INFO, logger="app.services.ingestion"):
        with app_logging():
            logging.getLogger("app.services.ingestion").info("accepted server_ms=%.1f", 1.5)
            logging.getLogger("app.services.ingestion").debug("hidden")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO app\.services\.ingestion: "
        r"accepted server_ms=1\.5",
        lines[0],
    )
    # Records still propagate, so caplog-based assertions keep working.
    assert "accepted server_ms=1.5" in caplog.text
    assert (logger.handlers, logger.level) == (handlers, level)


def test_security_events_carry_their_fields_in_the_formatted_line(capsys):
    from app.services.security_events import (
        record_admin_session_revoked,
        record_invalid_credential,
    )

    with app_logging():
        record_invalid_credential(
            "admin", endpoint="/api/v1/admin/x\nFORGED line", client_ip="203.0.113.7"
        )
        record_admin_session_revoked(
            "change_password_failures",
            session_id="01a1cd13-e9ee-7a69-927d-37aa9db216cd",
            endpoint="/api/v1/admin/auth/change-password",
            client_ip=None,
        )
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(
        "WARNING app.services.security_events: API credential rejected credential_kind=admin "
        "endpoint='/api/v1/admin/x\\nFORGED line' client_ip='203.0.113.7'"
    )
    assert lines[1].endswith(
        "WARNING app.services.security_events: Admin session revoked "
        "reason=change_password_failures session_id=01a1cd13-e9ee-7a69-927d-37aa9db216cd "
        "endpoint='/api/v1/admin/auth/change-password' client_ip='unknown'"
    )
