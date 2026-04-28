import pytest

from scripts.render_nginx_source_allowlist import render_source_allowlist


def test_render_source_allowlist_from_comma_separated_cidrs():
    rendered = render_source_allowlist("203.0.113.50/32, 10.0.0.0/8")

    assert "allow 203.0.113.50/32;" in rendered
    assert "allow 10.0.0.0/8;" in rendered
    assert rendered.endswith("deny all;\n")


def test_render_source_allowlist_rejects_empty_cidrs():
    with pytest.raises(ValueError, match="SOURCE_ALLOWLIST_CIDRS"):
        render_source_allowlist(" , ")


def test_render_source_allowlist_rejects_invalid_cidr():
    with pytest.raises(ValueError):
        render_source_allowlist("not-a-cidr")
