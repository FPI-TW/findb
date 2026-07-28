import re

from scripts.render_nginx_serve_key import (
    DEFAULT_REFERER_REGEX,
    render_serve_key,
)


def test_default_referer_allows_legacy_and_dashboard_lookup_routes() -> None:
    allowed = [
        "https://findb.tingfong.com/instrument-lookup",
        "https://findb.tingfong.com/instrument-lookup?ds=macro",
        "https://findb.tingfong.com/dashboard/lookup",
        "https://findb.tingfong.com/dashboard/lookup?ds=instruments",
    ]
    rejected = [
        "https://findb.tingfong.com/dashboard/",
        "https://findb.tingfong.com/dashboard/lookup-impersonator",
        "https://findb.tingfong.com.evil.example/dashboard/lookup",
        "https://evil.example/dashboard/lookup",
    ]

    assert all(re.search(DEFAULT_REFERER_REGEX, referer) for referer in allowed)
    assert not any(re.search(DEFAULT_REFERER_REGEX, referer) for referer in rejected)


def test_rendered_map_injects_dedicated_lookup_key() -> None:
    rendered = render_serve_key("lookup-key", DEFAULT_REFERER_REGEX)

    assert f'"~{DEFAULT_REFERER_REGEX}"  "lookup-key";' in rendered
    assert "FINDB_LOOKUP_SERVE_API_KEY" in rendered
    assert "default" in rendered
    assert "$http_x_api_key" in rendered


def test_rendered_map_falls_back_to_caller_header_without_keys() -> None:
    rendered = render_serve_key("", DEFAULT_REFERER_REGEX)

    assert "FINDB_LOOKUP_SERVE_API_KEY is empty" in rendered
    assert "default $http_x_api_key;" in rendered


def test_lookup_key_rejects_unsafe_nginx_characters() -> None:
    for unsafe_key in ('bad"key', "bad\\key", "bad\nkey", "bad\rkey"):
        try:
            render_serve_key(unsafe_key, DEFAULT_REFERER_REGEX)
        except ValueError as exc:
            assert "FINDB_LOOKUP_SERVE_API_KEY" in str(exc)
        else:
            raise AssertionError(f"unsafe key was accepted: {unsafe_key!r}")
