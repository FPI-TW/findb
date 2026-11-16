import ipaddress
from pathlib import Path

from scripts.render_nginx_cloudflare_real_ip import (
    DOCKER_PORT_PROXY_CIDRS,
    FALLBACK_CIDRS,
    render_cloudflare_real_ip,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONFIG = REPO_ROOT / "infra" / "nginx" / "nginx.conf"


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


def test_connections_relayed_by_the_docker_port_proxy_keep_their_cloudflare_client():
    """The first connections after nginx starts arrive from the bridge gateway.

    Without trusting it, every request on such a connection is judged by the
    gateway address and the Source allowlist rejects an allowlisted client.
    """
    for fetch_text in (_official, _unavailable):
        rendered = render_cloudflare_real_ip(fetch_text)
        for cidr in DOCKER_PORT_PROXY_CIDRS:
            assert f"set_real_ip_from {cidr};" in rendered

    sample = (REPO_ROOT / "infra" / "nginx" / "cloudflare-real-ip.conf").read_text(encoding="utf-8")
    for cidr in DOCKER_PORT_PROXY_CIDRS:
        assert f"set_real_ip_from {cidr};" in sample


def test_only_private_bridge_ranges_are_trusted_beyond_cloudflare():
    networks = [ipaddress.ip_network(cidr) for cidr in DOCKER_PORT_PROXY_CIDRS]

    # Never a public range: a public peer must be Cloudflare to have its header believed.
    assert all(network.is_private for network in networks)
    assert not any(
        network.overlaps(ipaddress.ip_network(cloudflare))
        for network in networks
        for cloudflare in FALLBACK_CIDRS
    )
    # The bridge gateway seen on the deployed hosts and Docker Desktop's.
    for gateway in ("172.18.0.1", "192.168.65.1"):
        assert any(ipaddress.ip_address(gateway) in network for network in networks)
    # A VPC address is not a Docker bridge and stays untrusted.
    assert not any(ipaddress.ip_address("10.0.1.25") in network for network in networks)


def _official(url: str) -> str:
    return "2001:db8::/32\n" if url.endswith("/ips-v6") else "203.0.113.0/24\n"


def _unavailable(_url: str) -> str:
    raise OSError("network unavailable")
