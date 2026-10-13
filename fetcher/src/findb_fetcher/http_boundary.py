"""Concrete synchronous HTTP boundaries, before application response hooks."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import httpx

from findb_fetcher.full_market_state import QuotaBlockedError

_guard = threading.Lock()


def prepare_transport(client: httpx.Client, url: str) -> None:
    transport = client._transport_for_url(httpx.URL(url))
    if type(transport) is httpx.HTTPTransport:
        return
    if type(transport) is not httpx.MockTransport:
        raise QuotaBlockedError("HTTP transport lacks a supported invocation boundary")
    with _guard:
        if getattr(transport, "_findb_boundary", False):
            return
        original = transport.handle_request

        def handle(request: httpx.Request) -> httpx.Response:
            request.read()
            start = request.extensions.get("findb_start")
            if start is not None:
                start()
            response = original(request)
            headers = request.extensions.get("findb_headers")
            if headers is not None:
                headers(response.status_code, response.headers)
            return response

        transport.handle_request = handle  # type: ignore[method-assign]
        transport._findb_boundary = True  # type: ignore[attr-defined]


def extensions(
    *,
    start: Callable[[], None] | None = None,
    headers: Callable[[int, httpx.Headers], None] | None = None,
) -> dict[str, Any]:
    tunnel = False

    def trace(event: str, info: dict[str, Any]) -> None:
        nonlocal tunnel
        if event.endswith("send_request_headers.started"):
            # HTTP CONNECT prepares a tunnel; it is not the provider request.
            tunnel = getattr(info.get("request"), "method", None) == b"CONNECT"
            if start is not None and not tunnel:
                start()
        if (
            event.endswith("receive_response_headers.complete")
            and headers is not None
            and not tunnel
        ):
            _, status, _, raw_headers = info["return_value"]
            headers(status, httpx.Headers(raw_headers))

    return {"trace": trace, "findb_start": start, "findb_headers": headers}
