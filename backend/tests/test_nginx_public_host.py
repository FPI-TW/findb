import pytest

from scripts.render_nginx_public_host import (
    PUBLIC_HOST_PLACEHOLDER,
    render_public_host,
)


def test_render_public_host_replaces_each_server_name() -> None:
    rendered = render_public_host(
        f"server_name {PUBLIC_HOST_PLACEHOLDER};\nserver_name {PUBLIC_HOST_PLACEHOLDER};\n",
        "findb-staging.tingfong.com",
    )

    assert rendered.count("server_name findb-staging.tingfong.com;") == 2


@pytest.mark.parametrize("host", ["https://findb.example.com", "host; return 200", "localhost"])
def test_render_public_host_rejects_non_dns_hostname(host: str) -> None:
    with pytest.raises(ValueError, match="valid DNS hostname"):
        render_public_host(PUBLIC_HOST_PLACEHOLDER * 2, host)
