"""Nginx Source body limits must match the Backend limits for whole-cohort requests."""

import re
from pathlib import Path

from app.main import SOURCE_UNIVERSE_MAX_PAYLOAD_BYTES

NGINX_CONFIG = Path(__file__).resolve().parents[2] / "infra" / "nginx" / "nginx.conf"
MANIFEST_LOCATION = "location ~ ^/api/v1/source/provider-work/[^/]+/manifest$"


def _block(config: str, header: str) -> str:
    """Return the body of the brace block that follows ``header``."""
    start = config.index(header + " {") + len(header) + 2
    depth = 1
    for index in range(start, len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[start:index]
    raise AssertionError(f"unterminated block: {header}")


def _own_directives(body: str) -> str:
    """Drop nested blocks so only this location's own directives remain."""
    while (nested := re.search(r"\n\s*location [^{]*\{", body)) is not None:
        inner = _block(body, nested.group(0).strip()[:-2].rstrip())
        body = body.replace(nested.group(0) + inner + "}", "")
    return body


def test_only_whole_cohort_source_routes_raise_the_body_limit():
    config = NGINX_CONFIG.read_text(encoding="utf-8")
    large = f"client_max_body_size {SOURCE_UNIVERSE_MAX_PAYLOAD_BYTES // (1024 * 1024)}m;"

    assert SOURCE_UNIVERSE_MAX_PAYLOAD_BYTES == 16 * 1024 * 1024
    assert config.count(large) == 2
    assert large in _block(config, "location = /api/v1/source/universes")
    source = _block(config, "location ^~ /api/v1/source/")
    assert "client_max_body_size 1m;" in _own_directives(source)
    # `^~` stops server-level regex matching, so the manifest route must be nested.
    assert MANIFEST_LOCATION not in config.replace(source, "")
    manifest = _block(source, MANIFEST_LOCATION)
    assert large in manifest
    for directive in (
        "include /etc/nginx/source-allowlist.conf;",
        "error_page 403 = @source_forbidden;",
        "proxy_pass http://$findb_upstream;",
        "set $findb_upstream ingest:8080;",
        "proxy_set_header X-Forwarded-For $remote_addr;",
    ):
        assert directive in manifest


def test_manifest_location_matches_only_the_manifest_route():
    pattern = re.compile(MANIFEST_LOCATION.removeprefix("location ~ "))

    assert pattern.fullmatch("/api/v1/source/provider-work/0197-abc/manifest")
    for path in (
        "/api/v1/source/provider-work/0197-abc/manifest/extra",
        "/api/v1/source/provider-work/0197-abc/prepare",
        "/api/v1/source/provider-work/a/b/manifest",
        "/api/v1/source/ingest",
    ):
        assert not pattern.fullmatch(path)
