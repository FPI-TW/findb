from pathlib import Path

from scripts.render_nginx_cloudflare_real_ip import (
    FALLBACK_CIDRS,
    render_cloudflare_real_ip,
)

NGINX_CONFIG = Path("infra/nginx/nginx.conf")


def test_nginx_includes_cloudflare_real_ip_config():
    config = NGINX_CONFIG.read_text(encoding="utf-8")

    assert "include /etc/nginx/cloudflare-real-ip.conf;" in config


def test_render_cloudflare_real_ip_from_official_lists():
    def fetch_text(url: str) -> str:
        if url.endswith("/ips-v4"):
            return "203.0.113.5/24\n198.51.100.0/24\n"
        if url.endswith("/ips-v6"):
            return "2001:db8::/32\n"
        raise AssertionError(f"unexpected URL: {url}")

    rendered = render_cloudflare_real_ip(fetch_text)

    assert "real_ip_header CF-Connecting-IP;" in rendered
    assert "real_ip_recursive on;" in rendered
    assert "set_real_ip_from 203.0.113.0/24;" in rendered
    assert "set_real_ip_from 198.51.100.0/24;" in rendered
    assert "set_real_ip_from 2001:db8::/32;" in rendered


def test_render_cloudflare_real_ip_falls_back_when_fetch_fails(capsys):
    def fetch_text(_url: str) -> str:
        raise OSError("network unavailable")

    rendered = render_cloudflare_real_ip(fetch_text)

    for cidr in FALLBACK_CIDRS:
        assert f"set_real_ip_from {cidr};" in rendered
    assert "fallback Cloudflare IPv4 CIDRs" in rendered
    assert "failed to fetch Cloudflare IP ranges" in capsys.readouterr().err


def test_render_cloudflare_real_ip_falls_back_on_invalid_official_response(capsys):
    def fetch_text(_url: str) -> str:
        return "not-a-cidr\n"

    rendered = render_cloudflare_real_ip(fetch_text)

    assert f"set_real_ip_from {FALLBACK_CIDRS[0]};" in rendered
    assert "failed to fetch Cloudflare IP ranges" in capsys.readouterr().err
