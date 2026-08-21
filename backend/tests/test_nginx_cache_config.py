import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONFIG = REPO_ROOT / "infra" / "nginx" / "nginx.conf"
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
EXPECTED_NGINX_IMAGE = (
    "nginx:1.31.4-alpine@sha256:db35bfc6b2951e7f8a72db5db120288c127ffaeeb4a6d4b95a26fead017d5913"
)
NGINX_IMAGE_PATTERN = re.compile(
    r"^nginx:(?P<version>\d+\.\d+\.\d+)-alpine@sha256:(?P<digest>[0-9a-f]{64})$"
)


def test_nginx_serve_api_does_not_cache_protected_responses():
    config = NGINX_CONFIG.read_text(encoding="utf-8")

    assert "location ^~ /api/v1/serve/" in config
    assert "proxy_cache" not in config
    assert "proxy_cache_key" not in config
    assert "$http_x_findb_key_tier" not in config


def test_prod_compose_shares_generated_static_cache_between_roles():
    compose = PROD_COMPOSE.read_text(encoding="utf-8")

    assert "findb-static-data:/app/app/static/data" in compose
    assert compose.count("findb-static-data:/app/app/static/data") == 2
    assert "FINDB_STATIC_CACHE_BASE_URL" in compose
    assert "http://serve:${PORT" in compose


def test_prod_compose_pins_nginx_to_explicit_version_and_repository_digest():
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    image = compose["services"]["nginx"].get("image")

    assert isinstance(image, str), "Production nginx service must declare an image"
    match = NGINX_IMAGE_PATTERN.fullmatch(image)
    assert match is not None, (
        "Production nginx image must use an explicit semantic version, the -alpine "
        "variant, and a 64-hex repository sha256 digest; floating or tag-only "
        "references are forbidden"
    )
    assert image == EXPECTED_NGINX_IMAGE


def test_dashboard_assets_do_not_buffer_upstream_responses_to_disk():
    config = NGINX_CONFIG.read_text(encoding="utf-8")

    asset_location = config.split("location ^~ /dashboard/assets/ {", 1)[1].split("\n    }", 1)[0]
    dashboard_location = config.split("location ^~ /dashboard/ {", 1)[1].split("\n    }", 1)[0]

    assert "proxy_max_temp_file_size 0;" in asset_location
    assert "proxy_max_temp_file_size 0;" not in dashboard_location
    assert config.index("location ^~ /dashboard/assets/ {") < config.index(
        "location ^~ /dashboard/ {"
    )
