from pathlib import Path


def test_nginx_serve_api_does_not_cache_protected_responses():
    config = Path("infra/nginx/nginx.conf").read_text(encoding="utf-8")

    assert "location ^~ /api/v1/serve/" in config
    assert "proxy_cache" not in config
    assert "proxy_cache_key" not in config
    assert "$http_x_findb_key_tier" not in config


def test_prod_compose_shares_generated_static_cache_between_roles():
    compose = Path("docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "findb-static-data:/app/app/static/data" in compose
    assert compose.count("findb-static-data:/app/app/static/data") == 2
    assert "FINDB_STATIC_CACHE_BASE_URL" in compose
    assert "http://serve:${PORT" in compose
