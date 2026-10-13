"""Real subprocess/production child protocol with a local recording SDK double."""

import subprocess

_NATIVE_POPEN = subprocess.Popen

SDK_CHILD = r"""
import hashlib, json, os, sys, time, types
from findb_fetcher.providers import shioaji_session as ss
output, mode = sys.argv[2], sys.argv[3]
def emit(event, **values):
    with open(output, "a") as stream:
        stream.write(json.dumps({"event": event, "at": time.time(), **values}) + "\n")
emit("environment", environment_present=sorted(set(os.environ) & {"SOURCE_CLIENT_KEY", "TWELVE_DATA_API_KEY", "FINLAB_API_TOKEN", "AWS_SECRET_ACCESS_KEY", "SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"}))
class Stocks:
    def __getattr__(self, exchange):
        emit("contracts", exchange=exchange)
        if mode == "contracts-slow":
            time.sleep(.4)
        return [types.SimpleNamespace(code="2330" if exchange == "TSE" else "006201", exchange=exchange)]
class API:
    def __init__(self, simulation):
        emit("create", simulation=simulation)
        if mode == "create-slow": time.sleep(.4)
        self.logged = False
        self.Contracts = types.SimpleNamespace(Stocks=Stocks())
    def login(self, api_key, secret_key, subscribe_trade):
        emit("login", credential_sha256=hashlib.sha256(api_key.encode()).hexdigest())
        if mode == "login-slow": time.sleep(.4)
        if mode == "login-fail": raise RuntimeError("private-provider-marker")
        self.logged = True
    def usage(self):
        emit("usage")
        if not self.logged or mode == "unavailable": raise RuntimeError("usage unavailable")
        connections = 0 if mode == "connections-zero" else (6 if mode == "connections-invalid" else (5 if mode == "connections-full" else 1))
        return {"bytes": 524288000 if mode == "bytes-full" else 10, "limit_bytes": 524288000, "remaining_bytes": 0 if mode == "bytes-full" else 524287990, "connections": connections}
    def kbars(self, contract, **kwargs):
        emit("kbars", start=time.time())
        if mode == "kbars-fail": raise RuntimeError("private-provider-marker")
        if mode == "kbars-slow": time.sleep(.4)
        emit("kbars-complete", complete=time.time())
        return {key: [] for key in ("ts", "Open", "High", "Low", "Close", "Volume", "Amount")}
    def logout(self):
        emit("logout")
        self.logged = False
sdk = types.SimpleNamespace(Shioaji=API)
original_import = ss.importlib.import_module
ss.importlib.import_module = lambda name: sdk if name == "shioaji" else original_import(name)
ss.importlib.metadata.version = lambda name: "1.7.1"
ss._entry()
"""


def install_launcher(monkeypatch, output, mode="normal"):
    import sys

    from findb_fetcher.providers import shioaji_session

    original = _NATIVE_POPEN

    def launch(args, **kwargs):
        assert "SOURCE_CLIENT_KEY" not in kwargs["env"]
        return original([sys.executable, "-c", SDK_CHILD, args[-1], str(output), mode], **kwargs)

    monkeypatch.setattr(shioaji_session.subprocess, "Popen", launch)
