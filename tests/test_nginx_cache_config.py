from pathlib import Path


def test_nginx_serve_api_uses_micro_cache():
    config = Path("infra/nginx/nginx.conf").read_text(encoding="utf-8")

    assert "proxy_cache_path /var/cache/nginx/findb_serve" in config
    assert "keys_zone=findb_serve_cache:20m" in config
    assert "location ^~ /api/v1/serve/" in config
    assert "proxy_cache findb_serve_cache;" in config
    assert "proxy_cache_methods GET HEAD;" in config
    assert "$findb_serve_cache_partition" in config
    assert "add_header X-Cache-Status $upstream_cache_status always;" in config


def test_nginx_cache_key_is_partitioned_by_tier_or_api_key():
    config = Path("infra/nginx/nginx.conf").read_text(encoding="utf-8")

    assert "map $http_x_findb_key_tier $findb_serve_cache_partition" in config
    assert '"" $findb_serve_proxy_key;' in config
