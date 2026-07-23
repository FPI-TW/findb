import pytest

from scripts.render_nginx_source_allowlist import render_source_allowlist


def test_render_source_allowlist_from_comma_separated_cidrs():
    rendered = render_source_allowlist("203.0.113.50/32, 10.0.0.0/8")

    assert "allow 127.0.0.1/32;" in rendered
    assert "allow ::1/128;" in rendered
    assert "allow 203.0.113.50/32;" in rendered
    assert "allow 10.0.0.0/8;" in rendered
    assert rendered.endswith("deny all;\n")


def test_render_source_allowlist_allows_localhost_when_cidrs_are_empty():
    rendered = render_source_allowlist(" , ")

    assert "allow 127.0.0.1/32;" in rendered
    assert "allow ::1/128;" in rendered
    assert rendered.endswith("deny all;\n")


def test_render_source_allowlist_dedupes_localhost_cidrs():
    rendered = render_source_allowlist("127.0.0.1/32, ::1/128, 203.0.113.50/32")

    assert rendered.count("allow 127.0.0.1/32;") == 1
    assert rendered.count("allow ::1/128;") == 1
    assert "allow 203.0.113.50/32;" in rendered


def test_render_source_allowlist_normalizes_single_ip_to_host_cidr():
    rendered = render_source_allowlist("203.0.113.50")

    assert "allow 203.0.113.50/32;" in rendered


def test_render_source_allowlist_rejects_invalid_cidr():
    with pytest.raises(ValueError):
        render_source_allowlist("not-a-cidr")
