from pathlib import Path

NGINX_CONFIG = Path("infra/nginx/nginx.conf")


def test_nginx_trusts_cloudflare_connecting_ip():
    config = NGINX_CONFIG.read_text(encoding="utf-8")

    assert "real_ip_header CF-Connecting-IP;" in config
    assert "real_ip_recursive on;" in config
    assert "set_real_ip_from 172.64.0.0/13;" in config
    assert "set_real_ip_from 2a06:98c0::/29;" in config
