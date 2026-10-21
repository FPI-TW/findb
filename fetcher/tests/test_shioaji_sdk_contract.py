"""The installed Shioaji SDK still exposes every API surface the adapter uses.

Other Shioaji tests use offline doubles, so an SDK upgrade that renames or
reshapes one of these bindings would otherwise only fail against the live
service. Nothing here constructs a client, logs in or touches the network.
"""

import importlib.metadata
import inspect
import os

import pytest

from findb_fetcher.providers.shioaji import _REVIEWED_BASE_CONTRACTS, SHIOAJI_SDK_VERSION

# CI installs the extra; a local run without it skips instead of failing.
sdk = __import__("shioaji") if os.getenv("CI") else pytest.importorskip("shioaji")
core = sdk._core


def _parameters(function):
    return inspect.signature(function).parameters


def test_installed_sdk_is_the_reviewed_release():
    assert importlib.metadata.version("shioaji") == SHIOAJI_SDK_VERSION


@pytest.mark.parametrize(
    "method,required,keywords",
    [
        ("login", ["self", "api_key", "secret_key"], ["subscribe_trade"]),
        ("logout", ["self"], []),
        ("usage", ["self"], []),
        ("kbars", ["self", "contract"], ["start", "end"]),
    ],
)
def test_client_methods_accept_adapter_calls(method, required, keywords):
    parameters = _parameters(getattr(sdk.Shioaji, method))
    assert [
        name for name, item in parameters.items() if item.default is inspect.Parameter.empty
    ] == required
    assert set(keywords) <= set(parameters)


def test_contract_access_bindings():
    # Catalogue loading reads api.Contracts.Stocks.<exchange>; lookups use api.contracts.get.
    assert {"contracts", "Contracts"} <= set(dir(sdk.Shioaji))
    assert "Stocks" in dir(core.Contracts)
    assert core.ContractCategory.__getattribute__ is not object.__getattribute__
    assert list(_parameters(core.ContractsApi.get))[:2] == ["self", "code"]


def test_kbars_and_usage_fields():
    assert {"ts", "Open", "High", "Low", "Close", "Volume", "Amount", "dict"} <= set(
        dir(core.KBars)
    )
    assert {"bytes", "limit_bytes", "remaining_bytes", "connections"} <= set(dir(core.UsageOut))


@pytest.mark.parametrize("symbol", sorted(_REVIEWED_BASE_CONTRACTS))
def test_reviewed_base_contracts_construct(symbol):
    security_type, exchange, region = _REVIEWED_BASE_CONTRACTS[symbol]
    contract = sdk.BaseContract(
        security_type=security_type, exchange=exchange, code=symbol, region=region
    )
    assert (contract.security_type, contract.exchange, contract.code, contract.region) == (
        security_type,
        exchange,
        symbol,
        region,
    )
