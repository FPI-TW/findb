"""The HTTPS access log carries request and upstream timing for Source diagnostics."""

import re

from tests.test_nginx_cache_config import NGINX_CONFIG


def test_https_access_log_records_request_and_upstream_timing():
    config = NGINX_CONFIG.read_text()
    log_format = re.search(r"log_format findb_timing (.*?);", config, re.S)
    assert log_format is not None
    fields = log_format.group(1)
    for field in (
        "rt=$request_time",
        "urt=$upstream_response_time",
        "uct=$upstream_connect_time",
        "cf_ray=$http_cf_ray",
    ):
        assert field in fields
    # Credentials never enter the log: no API key or authorization header fields.
    assert "x_api_key" not in fields.lower() and "authorization" not in fields.lower()
    https = config[config.index("listen 443 ssl;") :]
    assert "access_log /var/log/nginx/access.log findb_timing;" in https.split("location", 1)[0]
