"""Exact HTTP response bytes with identity encoding and bounded reads."""

from __future__ import annotations

import httpx


class BoundedResponseError(RuntimeError):
    def __init__(self, message: str, *, observed_bytes: int = 0) -> None:
        self.observed_bytes = observed_bytes
        super().__init__(message)


def read_identity_response(response: httpx.Response, *, bound: int) -> bytes:
    encoding = response.headers.get("content-encoding", "identity")
    if encoding.strip().lower() != "identity":
        raise BoundedResponseError("unsupported Content-Encoding")
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise BoundedResponseError("invalid Content-Length") from exc
        if declared_length < 0:
            raise BoundedResponseError("invalid Content-Length")
        if declared_length > bound:
            raise BoundedResponseError("response exceeds the size limit")
    raw = bytearray()
    chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
    for chunk in chunks:
        observed = len(raw) + len(chunk)
        if observed > bound:
            raise BoundedResponseError("response exceeds the size limit", observed_bytes=observed)
        raw.extend(chunk)
    return bytes(raw)
