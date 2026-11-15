"""The exec'd worker against the installed, pinned FinLab SDK.

Ordinary CI does not install the optional SDK and skips this module; the other
FinLab tests model the SDK with fakes. Here the real SDK runs with its HTTP
transport replaced inside the worker and every socket refused, so nothing
leaves the machine. Each scenario runs in its own interpreter with a private
home directory: the SDK keeps process-global state and a cache below ``~``.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from findb_fetcher.providers.finlab import FINLAB_SDK_VERSION

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("finlab") is None, reason="optional FinLab SDK is not installed"
)

TOKEN = "t" * 64

# Imported by every interpreter that has this directory on PYTHONPATH.
TRANSPORT = """
import json
import pathlib
import socket

import requests

ROOT = pathlib.Path(__file__).parent
MODE = (ROOT / "mode").read_text()
QUOTA = "Usage exceed 5000 MB/day. Please consider upgrade to VIP program."


def refused(*args, **kwargs):
    raise OSError("offline test: sockets are refused")


socket.socket.connect = refused
socket.create_connection = refused


class Reply:
    def __init__(self, payload=None, content=b""):
        self.status_code, self.payload, self.content = 200, payload, content
        self.headers = {"content-length": str(len(content))}

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        yield self.content


def record(**fields):
    with open(ROOT / "requests.log", "a") as log:
        log.write(json.dumps(fields) + "\\n")


def post(url, *args, **kwargs):
    body = kwargs.get("json") or {}
    items = [item["blob_name"] for item in body.get("items", [])]
    record(
        method="POST",
        host=url.split("/")[2],
        credentials=sorted(key for key in body if key.endswith("token")),
        token=body.get("api_token"),
        items=items,
    )
    if MODE == "network":
        raise requests.ConnectionError("provider-words-that-must-not-leak")
    if MODE == "auth":
        return Reply({"error": "api_token not valid"})
    if MODE == "quota":
        return Reply({"error": QUOTA})
    if MODE == "item_quota":
        return Reply({"results": [{"blob_name": name, "error": QUOTA} for name in items]})
    return Reply(
        {
            "results": [
                {
                    "blob_name": name,
                    "url": "https://signed.invalid/" + name,
                    "time_created": "20261113134904",
                    "quota": 41.9,
                    "limit_size": 5000,
                    "role": "vip",
                    "billed_bytes": 1500000,
                }
                for name in items
            ]
        }
    )


def get(url, *args, **kwargs):
    record(method="GET", host=url.split("/")[2])
    if url.startswith("https://signed.invalid/"):
        return Reply(content=(ROOT / "table.feather").read_bytes())
    raise requests.ConnectionError("offline test")


requests.post, requests.get = post, get
"""

# Stands in for the long-lived runtime: launches the worker and maps its frame.
DRIVER = """
import datetime
import json
import os
import pathlib
import sys

import pandas as pd

from findb_fetcher import provider_process
from findb_fetcher.providers import finlab

root, account = pathlib.Path(sys.argv[1]), sys.argv[2] == "account"
days = pd.to_datetime(["2026-11-11", "2026-11-12", "2026-11-13"])
table = pd.DataFrame(
    {"2330": [1.5, 2.5, None], "1101": [4.0, None, 6.0], "9999": [7.0, 8.0, 9.0]},
    index=pd.Index(days, name="date"),
)
table.reset_index().to_feather(root / "table.feather")
environment = provider_process.provider_environment
provider_process.provider_environment = lambda: {**environment(), "PYTHONPATH": str(root)}
payload = {
    "token": sys.argv[3],
    "dataset": "price:收盤價",
    "symbols": ("2330", "1101", "0050"),
    "account": account,
}
result = {}
for attempt in range(int(sys.argv[4])):
    try:
        frame, observed, usage = provider_process.supervised_call("finlab.get", payload, timeout=60)
    except provider_process.ProviderProcessError as exc:
        result = {"code": exc.code}
        break
    mapped = finlab._table_from_dataframe(
        frame,
        target_date=datetime.date(2026, 11, 13),
        reviewed_symbols=("2330", "1101"),
        carry_previous=True,
    )
    result = {
        "columns": list(frame.columns),
        "dates": list(mapped.dates),
        "symbols": list(mapped.symbols),
        "values": [None if finlab.unobserved(value) else float(value) for value in mapped.values[0]],
        "previous": [list(item) for item in mapped.previous],
        "catalogue": list(
            finlab._catalogue_from_dataframe(frame, reviewed_symbols=("0050", "1101")).symbols
        ),
        "observed": observed,
        "expected_observed": finlab._dataframe_response_bytes(table),
        "usage": usage,
    }
log = root / "requests.log"
result["requests"] = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
cache = pathlib.Path.home() / "finlab_db"
result["cache"] = sorted(path.name for path in cache.iterdir()) if cache.is_dir() else []
print(json.dumps(result))
"""


def _run(tmp_path: Path, mode: str, *, account: bool = False, calls: int = 1) -> dict:
    root = tmp_path / "transport"
    root.mkdir()
    (root / "sitecustomize.py").write_text(textwrap.dedent(TRANSPORT))
    (root / "mode").write_text(mode)
    home = tmp_path / "home"
    home.mkdir()
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(DRIVER),
            str(root),
            "account" if account else "plain",
            TOKEN,
            str(calls),
        ],
        env={**os.environ, "HOME": str(home), "PROVIDER_WORK_REQUIRED": "true"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.splitlines()[-1])


def test_the_installed_sdk_is_the_reviewed_release_and_shape(tmp_path) -> None:
    assert importlib.metadata.version("finlab") == FINLAB_SDK_VERSION
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from findb_fetcher.providers.finlab import verify_sdk_contract; verify_sdk_contract()",
        ],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]


@pytest.mark.parametrize("account", [False, True])
def test_a_full_download_becomes_one_reviewed_target_row(tmp_path, account) -> None:
    result = _run(tmp_path, "ok", account=account)

    # The table reaches the runtime narrowed to the reviewed symbols it serves.
    assert result["columns"] == ["2330", "1101"]
    assert result["dates"] == ["2026-11-13"] and result["symbols"] == ["2330", "1101"]
    assert result["values"] == [None, 6.0]
    assert result["previous"] == [["2330", "2026-11-12", 2.5]]
    assert result["catalogue"] == ["1101"]
    # Accounted bytes describe the complete table, not the narrowed one.
    assert result["observed"] == (result["expected_observed"] if account else None)
    assert result["usage"] == {"used_mb": 41.9, "limit_mb": 5000.0, "billed_bytes": 1500000}
    posts = [request for request in result["requests"] if request["method"] == "POST"]
    # The API token is the only identity sent, and the version check never asks PyPI.
    assert [(post["credentials"], post["token"]) for post in posts] == [(["api_token"], TOKEN)]
    assert posts[0]["items"] == ["price#收盤價.feather"]
    assert "pypi.org" not in {request["host"] for request in result["requests"]}
    assert "price#收盤價.feather" in result["cache"]


def test_a_cached_table_is_updated_instead_of_downloaded_again(tmp_path) -> None:
    result = _run(tmp_path, "ok", calls=2)

    posts = [request["items"] for request in result["requests"] if request["method"] == "POST"]
    # The second worker offers its cached table and asks for the update first. The
    # stand-in update does not carry the published hash, so the SDK falls back to
    # the full table; both replies are observed and the last usage is reported.
    assert posts == [
        ["price#收盤價.feather"],
        ["price#收盤價.recent.feather"],
        ["price#收盤價.feather"],
    ]
    assert result["values"] == [None, 6.0]
    assert result["usage"] == {"used_mb": 41.9, "limit_mb": 5000.0, "billed_bytes": 3000000}


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        # Left alone, the SDK answers an auth error by opening its browser login.
        ("auth", "auth"),
        ("quota", "quota"),
        ("item_quota", "quota"),
        ("network", "network"),
    ],
)
def test_an_api_refusal_keeps_its_class_without_any_login_prompt(tmp_path, mode, code) -> None:
    result = _run(tmp_path, mode)

    assert result["code"] == code
    assert result["cache"] == [] or "price#收盤價.feather" not in result["cache"]
    assert {request["host"] for request in result["requests"]} <= {
        "asia-east2-fdata-299302.cloudfunctions.net",
        "r2.finlab.finance",
    }
