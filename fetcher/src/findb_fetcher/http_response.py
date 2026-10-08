"""Exact HTTP response bytes with identity encoding and bounded reads."""

from __future__ import annotations

import httpx


class BoundedResponseError(RuntimeError):
    def __init__(self, message: str, *, observed_bytes: int = 0, declared_bytes: int = 0) -> None:
        self.observed_bytes = observed_bytes
        self.declared_bytes = declared_bytes
        super().__init__(message)


def read_identity_response(response: httpx.Response, *, bound: int) -> bytes:
    encoding = response.headers.get("content-encoding", "identity")
    identity = encoding.strip().lower() == "identity"
    received = (
        (len(response.content) if identity else response.num_bytes_downloaded)
        if response.is_stream_consumed
        else 0
    )
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise BoundedResponseError("invalid Content-Length", observed_bytes=received) from exc
        if declared_length < 0:
            raise BoundedResponseError("invalid Content-Length", observed_bytes=received)
        if declared_length > bound:
            raise BoundedResponseError(
                "response exceeds the size limit",
                # Buffered non-identity content may already be decoded. Its
                # length cannot stand in for actual received wire bytes.
                observed_bytes=received,
                declared_bytes=declared_length,
            )
    if not identity:
        raise BoundedResponseError("unsupported Content-Encoding", observed_bytes=received)
    raw = bytearray()
    chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
    try:
        for chunk in chunks:
            observed = len(raw) + len(chunk)
            if observed > bound:
                raise BoundedResponseError(
                    "response exceeds the size limit", observed_bytes=observed
                )
            raw.extend(chunk)
    except httpx.HTTPError as exc:
        # Preserve the transport error type and only account bytes actually read.
        exc.observed_bytes = len(raw)  # type: ignore[attr-defined]
        raise
    return bytes(raw)
