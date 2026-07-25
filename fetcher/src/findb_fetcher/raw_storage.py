"""Fetcher-owned Cloudflare R2 persistence for immutable provider response bytes."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    NoRegionError,
    PartialCredentialsError,
)

_ACCOUNT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9!_.*'()/=-]{1,512}$")
_DATASET_PATTERN = re.compile(r"^[a-z0-9_]{1,50}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_OBJECT_BYTES_HARD = 16 * 1024 * 1024
_MAX_REF_LENGTH = 2048


class RawStorageError(RuntimeError):
    """Base error for raw object persistence."""


class RawStorageConfigError(RawStorageError):
    """Raw object storage configuration is absent or unsafe."""


class RawStorageUploadError(RawStorageError):
    """Raw bytes could not be persisted safely."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RawStorageConfig:
    account_id: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    prefix: str = "raw/twelve-data"
    session_token: str | None = field(default=None, repr=False)
    max_object_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        account_id = self.account_id.strip().lower()
        bucket = self.bucket.strip()
        access_key_id = self.access_key_id.strip()
        secret_access_key = self.secret_access_key.strip()
        prefix = self.prefix.strip().strip("/")
        session_token = self.session_token.strip() if self.session_token else None
        if _ACCOUNT_ID_PATTERN.fullmatch(account_id) is None:
            raise RawStorageConfigError(
                "CLOUDFLARE_R2_ACCOUNT_ID must be a 32-character hexadecimal account ID"
            )
        if not _valid_bucket_name(bucket):
            raise RawStorageConfigError("CLOUDFLARE_R2_BUCKET is not a safe R2 bucket name")
        if not access_key_id:
            raise RawStorageConfigError("CLOUDFLARE_R2_ACCESS_KEY_ID is required")
        if not secret_access_key:
            raise RawStorageConfigError("CLOUDFLARE_R2_SECRET_ACCESS_KEY is required")
        if (
            _PREFIX_PATTERN.fullmatch(prefix) is None
            or "//" in prefix
            or any(segment in {".", ".."} for segment in prefix.split("/"))
        ):
            raise RawStorageConfigError("CLOUDFLARE_R2_PREFIX is not a safe object prefix")
        if not 1 <= self.max_object_bytes <= _MAX_OBJECT_BYTES_HARD:
            raise RawStorageConfigError(
                f"CLOUDFLARE_R2_MAX_OBJECT_BYTES must be between 1 and {_MAX_OBJECT_BYTES_HARD}"
            )

        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "bucket", bucket)
        object.__setattr__(self, "access_key_id", access_key_id)
        object.__setattr__(self, "secret_access_key", secret_access_key)
        object.__setattr__(self, "prefix", prefix)
        object.__setattr__(self, "session_token", session_token)

    @classmethod
    def from_env(cls) -> "RawStorageConfig":
        account_id = os.getenv("CLOUDFLARE_R2_ACCOUNT_ID", "")
        bucket = os.getenv("CLOUDFLARE_R2_BUCKET", "")
        access_key_id = os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID", "")
        secret_access_key = os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "")
        if not bucket.strip():
            raise RawStorageConfigError(
                "CLOUDFLARE_R2_BUCKET is required for Source delivery and scheduled execution"
            )
        return cls(
            account_id=account_id,
            bucket=bucket,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            prefix=os.getenv("CLOUDFLARE_R2_PREFIX", "raw/twelve-data"),
            session_token=os.getenv("CLOUDFLARE_R2_SESSION_TOKEN"),
            max_object_bytes=_positive_int_env("CLOUDFLARE_R2_MAX_OBJECT_BYTES", 8 * 1024 * 1024),
        )


@dataclass(frozen=True, slots=True)
class RawObject:
    ref: str
    sha256: str
    size_bytes: int


class RawPayloadStore(Protocol):
    def persist(
        self,
        raw_bytes: bytes,
        *,
        dataset_key: str,
        source_symbol: str,
    ) -> RawObject: ...


class R2RawPayloadStore:
    """Upload exact bytes with deterministic content-addressed R2 keys."""

    def __init__(self, config: RawStorageConfig, *, client: Any | None = None) -> None:
        self._config = config
        if client is None:
            import boto3

            try:
                client = boto3.client(
                    "s3",
                    endpoint_url=(f"https://{config.account_id}.r2.cloudflarestorage.com"),
                    aws_access_key_id=config.access_key_id,
                    aws_secret_access_key=config.secret_access_key,
                    aws_session_token=config.session_token,
                    region_name="auto",
                    config=BotoConfig(
                        signature_version="s3v4",
                        connect_timeout=5,
                        read_timeout=30,
                        retries={"mode": "standard", "max_attempts": 3},
                    ),
                )
            except Exception as exc:
                raise RawStorageConfigError("R2 raw storage client initialization failed") from exc
        self._client = client

    def persist(
        self,
        raw_bytes: bytes,
        *,
        dataset_key: str,
        source_symbol: str,
    ) -> RawObject:
        if not isinstance(raw_bytes, bytes):
            raise RawStorageUploadError("raw provider payload must be bytes")
        if not raw_bytes:
            raise RawStorageUploadError("raw provider payload must not be empty")
        if len(raw_bytes) > self._config.max_object_bytes:
            raise RawStorageUploadError("raw provider payload exceeds object size limit")
        if _DATASET_PATTERN.fullmatch(dataset_key) is None:
            raise RawStorageUploadError("dataset_key is not safe for raw object metadata")
        normalized_symbol = source_symbol.strip()
        if _SYMBOL_PATTERN.fullmatch(normalized_symbol) is None:
            raise RawStorageUploadError("source_symbol is not safe for raw object metadata")

        digest_bytes = hashlib.sha256(raw_bytes).digest()
        digest = digest_bytes.hex()
        symbol_digest = hashlib.sha256(normalized_symbol.encode("utf-8")).hexdigest()[:16]
        key = f"{self._config.prefix}/twelve_data/{dataset_key}/" f"{symbol_digest}/{digest}.json"
        ref = f"r2://{self._config.account_id}/{self._config.bucket}/{key}"
        if len(ref) > _MAX_REF_LENGTH:
            raise RawStorageUploadError("raw object reference exceeds ingress contract limit")

        arguments: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": key,
            "Body": raw_bytes,
            "ContentLength": len(raw_bytes),
            "ContentType": "application/json",
            "ChecksumSHA256": base64.b64encode(digest_bytes).decode("ascii"),
            "Metadata": {
                "sha256": digest,
                "provider": "twelve_data",
                "dataset": dataset_key,
                "source-symbol": normalized_symbol,
            },
        }
        try:
            self._client.put_object(**arguments)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = str(error.get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            retryable = (
                status == 429
                or (isinstance(status, int) and status >= 500)
                or code
                in {
                    "InternalError",
                    "RequestTimeout",
                    "ServiceUnavailable",
                    "SlowDown",
                    "Throttling",
                    "ThrottlingException",
                }
            )
            raise RawStorageUploadError(
                "raw provider payload upload failed",
                retryable=retryable,
            ) from exc
        except (NoCredentialsError, NoRegionError, PartialCredentialsError) as exc:
            raise RawStorageUploadError(
                "raw provider payload upload failed",
                retryable=False,
            ) from exc
        except BotoCoreError as exc:
            raise RawStorageUploadError(
                "raw provider payload upload failed",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise RawStorageUploadError("raw provider payload upload failed") from exc
        return RawObject(ref=ref, sha256=digest, size_bytes=len(raw_bytes))


def attach_raw_object(request: dict[str, Any], raw_object: RawObject) -> None:
    """Attach the credential-free immutable reference to an ingress batch."""
    attach_raw_provenance(
        request,
        source_raw_ref=raw_object.ref,
        source_raw_sha256=raw_object.sha256,
    )


def attach_raw_provenance(
    request: dict[str, Any],
    *,
    source_raw_ref: str | None,
    source_raw_sha256: str | None,
) -> None:
    """Attach an all-or-none provenance pair and derive a collision-safe identity."""
    if (source_raw_ref is None) != (source_raw_sha256 is None):
        raise RawStorageUploadError("raw object reference and checksum must be supplied together")
    if source_raw_ref is None or source_raw_sha256 is None:
        raise RawStorageUploadError("raw object provenance must not be empty")
    if _SHA256_PATTERN.fullmatch(source_raw_sha256) is None:
        raise RawStorageUploadError("raw object checksum must be lowercase SHA-256")
    if not _valid_r2_ref(source_raw_ref):
        raise RawStorageUploadError("raw object reference must be a bounded R2 URI")
    payload = request.get("payload")
    batch = payload.get("batch") if isinstance(payload, dict) else None
    if not isinstance(batch, dict):
        raise RawStorageUploadError("validated request is missing batch metadata")
    base_idempotency_key = request.get("idempotency_key")
    if not isinstance(base_idempotency_key, str) or not base_idempotency_key:
        raise RawStorageUploadError("validated request is missing idempotency identity")
    identity = hashlib.sha256(
        (base_idempotency_key + "\n" + source_raw_ref + "\n" + source_raw_sha256).encode("utf-8")
    ).hexdigest()
    dataset_key = request.get("dataset_key")
    if not isinstance(dataset_key, str):
        raise RawStorageUploadError("validated request is missing dataset identity")
    batch["source_raw_ref"] = source_raw_ref
    batch["source_raw_sha256"] = source_raw_sha256
    request["request_key"] = f"twelve_data:{dataset_key}:{identity[:32]}"
    request["idempotency_key"] = f"twelve_data:{identity}"


def require_raw_provenance(request: dict[str, Any]) -> None:
    """Reject persisted delivery state without a complete immutable raw pair."""
    payload = request.get("payload")
    batch = payload.get("batch") if isinstance(payload, dict) else None
    if not isinstance(batch, dict):
        raise RawStorageUploadError("validated request is missing batch metadata")
    ref = batch.get("source_raw_ref")
    sha256 = batch.get("source_raw_sha256")
    if not isinstance(ref, str) or not isinstance(sha256, str):
        raise RawStorageUploadError("prepared request is missing raw provenance")
    if not _valid_r2_ref(ref):
        raise RawStorageUploadError("prepared request raw reference is invalid")
    if _SHA256_PATTERN.fullmatch(sha256) is None:
        raise RawStorageUploadError("prepared request raw checksum is invalid")


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit():
        raise RawStorageConfigError(f"{name} must be a positive integer")
    value = int(raw)
    if value <= 0:
        raise RawStorageConfigError(f"{name} must be a positive integer")
    return value


def _valid_r2_ref(value: str) -> bool:
    if len(value) > _MAX_REF_LENGTH:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "r2"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname != parsed.netloc
        or _ACCOUNT_ID_PATTERN.fullmatch(parsed.netloc) is None
    ):
        return False
    path = parsed.path.removeprefix("/")
    bucket, separator, key = path.partition("/")
    return (
        bool(separator)
        and _valid_bucket_name(bucket)
        and bool(key)
        and "%" not in key
        and "\\" not in key
        and "//" not in key
        and all(segment not in {"", ".", ".."} for segment in key.split("/"))
    )


def _valid_bucket_name(value: str) -> bool:
    if _BUCKET_PATTERN.fullmatch(value) is None or ".." in value or ".-" in value or "-." in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return True
    return False
