"""Cloudflare R2 access for the FinDB roles that hold an R2 credential.

Each credential belongs to one role and one bucket of the same environment:
the Worker reads provider raw objects and publishes canonical objects, Serve
reads canonical objects. A role without a credential gets a configuration
error; it never falls back to another role's credential.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings, get_settings

_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Canonical keys are built by FinDB from validated identifiers only.
_CANONICAL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=/-]{0,1023}$")
_MAX_REF_LENGTH = 2048
_NOT_FOUND = frozenset({"404", "NoSuchKey", "NotFound"})
_PRECONDITION = frozenset({"409", "412", "PreconditionFailed", "ConditionalRequestConflict"})


class ObjectStorageError(RuntimeError):
    """Object storage failed; the message never carries a credential or a URL."""


class ObjectStorageConfigError(ObjectStorageError):
    """This role has no usable credential for the requested bucket."""


class ObjectNotFoundError(ObjectStorageError):
    """The object does not exist."""


class ObjectIntegrityError(ObjectStorageError):
    """The object is not the one its reference describes."""


class ObjectChangedError(ObjectStorageError):
    """The object was created or replaced since the caller last observed it."""


@dataclass(frozen=True, slots=True)
class R2Access:
    """One role's access to one bucket."""

    account_id: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if _ACCOUNT_ID.fullmatch(self.account_id) is None:
            raise ObjectStorageConfigError("R2 account id is not configured")
        if not _valid_bucket(self.bucket):
            raise ObjectStorageConfigError("R2 bucket is not configured")
        if not self.access_key_id or not self.secret_access_key:
            raise ObjectStorageConfigError("R2 credential is not configured for this role")


@dataclass(frozen=True, slots=True)
class StoredObject:
    """An object's bytes with the version the store reported for them."""

    body: bytes
    etag: str


def raw_reader_access(settings: Settings | None = None) -> R2Access:
    settings = settings or get_settings()
    return R2Access(
        settings.CLOUDFLARE_R2_ACCOUNT_ID,
        settings.CLOUDFLARE_R2_RAW_BUCKET,
        settings.CLOUDFLARE_R2_RAW_READER_ACCESS_KEY_ID,
        settings.CLOUDFLARE_R2_RAW_READER_SECRET_ACCESS_KEY,
        settings.CLOUDFLARE_R2_RAW_READER_SESSION_TOKEN or None,
    )


def canonical_publisher_access(settings: Settings | None = None) -> R2Access:
    settings = settings or get_settings()
    return R2Access(
        settings.CLOUDFLARE_R2_ACCOUNT_ID,
        settings.CLOUDFLARE_R2_CANONICAL_BUCKET,
        settings.CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID,
        settings.CLOUDFLARE_R2_CANONICAL_PUBLISHER_SECRET_ACCESS_KEY,
        settings.CLOUDFLARE_R2_CANONICAL_PUBLISHER_SESSION_TOKEN or None,
    )


def canonical_reader_access(settings: Settings | None = None) -> R2Access:
    settings = settings or get_settings()
    return R2Access(
        settings.CLOUDFLARE_R2_ACCOUNT_ID,
        settings.CLOUDFLARE_R2_CANONICAL_BUCKET,
        settings.CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID,
        settings.CLOUDFLARE_R2_CANONICAL_READER_SECRET_ACCESS_KEY,
        settings.CLOUDFLARE_R2_CANONICAL_READER_SESSION_TOKEN or None,
    )


def _client(access: R2Access) -> Any:
    import boto3

    try:
        return boto3.client(
            "s3",
            endpoint_url=f"https://{access.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access.access_key_id,
            aws_secret_access_key=access.secret_access_key,
            aws_session_token=access.session_token,
            region_name="auto",
            config=BotoConfig(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 4},
            ),
        )
    except Exception:
        raise ObjectStorageConfigError("R2 client initialization failed") from None


def _error_code(exc: ClientError) -> str:
    response = exc.response
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return str(response.get("Error", {}).get("Code") or status or "")


def _valid_bucket(value: str) -> bool:
    return (
        _BUCKET.fullmatch(value) is not None
        and ".." not in value
        and ".-" not in value
        and "-." not in value
    )


def _valid_key(key: str) -> bool:
    return (
        bool(key)
        and "%" not in key
        and "\\" not in key
        and "//" not in key
        and all(segment not in {"", ".", ".."} for segment in key.split("/"))
    )


def parse_raw_ref(ref: str) -> tuple[str, str, str]:
    """Split a credential-free ``r2://<account>/<bucket>/<key>`` reference."""
    if not isinstance(ref, str) or len(ref) > _MAX_REF_LENGTH:
        raise ObjectIntegrityError("raw object reference is not a bounded R2 URI")
    try:
        parsed = urlsplit(ref)
        port = parsed.port
    except ValueError:
        raise ObjectIntegrityError("raw object reference is not a bounded R2 URI") from None
    bucket, separator, key = parsed.path.removeprefix("/").partition("/")
    if (
        parsed.scheme != "r2"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname != parsed.netloc
        or _ACCOUNT_ID.fullmatch(parsed.netloc) is None
        or not separator
        or not _valid_bucket(bucket)
        or not _valid_key(key)
    ):
        raise ObjectIntegrityError("raw object reference is not a bounded R2 URI")
    return parsed.netloc, bucket, key


def _read(client: Any, bucket: str, key: str, *, max_bytes: int) -> StoredObject:
    """Read one object whose size the store reports as within ``max_bytes``."""
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        length = response.get("ContentLength")
        if type(length) is not int or not 0 <= length <= max_bytes:
            raise ObjectIntegrityError("object size is outside the accepted bound")
        # One byte more than declared proves the declared length without trusting it.
        body = response["Body"].read(length + 1)
    except ClientError as exc:
        if _error_code(exc) in _NOT_FOUND:
            raise ObjectNotFoundError("object does not exist") from None
        raise ObjectStorageError("object storage read failed") from None
    except BotoCoreError:
        raise ObjectStorageError("object storage read failed") from None
    if not isinstance(body, bytes) or len(body) != length:
        raise ObjectIntegrityError("object size does not match the stored length")
    return StoredObject(body, str(response.get("ETag") or ""))


class RawObjectReader:
    """Read-only access to the provider raw objects of this environment."""

    def __init__(self, access: R2Access | None = None, *, client: Any | None = None) -> None:
        self._access = access or raw_reader_access()
        self._client = client if client is not None else _client(self._access)

    def read(self, ref: str, *, sha256: str, max_bytes: int) -> bytes:
        """Return the bytes ``ref`` names, proven by size bound and SHA-256.

        A reference to another account or bucket is refused before any request:
        a delivery cannot point the Worker at an object outside its own raw bucket.
        """
        if _SHA256.fullmatch(sha256) is None:
            raise ObjectIntegrityError("raw object checksum must be lowercase SHA-256")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ObjectIntegrityError("raw object size bound must be positive")
        account_id, bucket, key = parse_raw_ref(ref)
        if account_id != self._access.account_id or bucket != self._access.bucket:
            raise ObjectIntegrityError("raw object reference is outside this environment")
        body = _read(self._client, bucket, key, max_bytes=max_bytes).body
        if hashlib.sha256(body).hexdigest() != sha256:
            raise ObjectIntegrityError("raw object does not match its checksum")
        return body


class CanonicalReader:
    """Read-only access to canonical objects (Serve)."""

    def __init__(self, access: R2Access | None = None, *, client: Any | None = None) -> None:
        self._access = access or canonical_reader_access()
        self._client = client if client is not None else _client(self._access)

    def read(self, key: str, *, max_bytes: int) -> StoredObject:
        _require_canonical_key(key)
        return _read(self._client, self._access.bucket, key, max_bytes=max_bytes)

    def version(self, key: str) -> str | None:
        """Return the stored version of ``key`` without its bytes, or ``None`` when absent."""
        _require_canonical_key(key)
        try:
            response = self._client.head_object(Bucket=self._access.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND:
                return None
            raise ObjectStorageError("object storage read failed") from None
        except BotoCoreError:
            raise ObjectStorageError("object storage read failed") from None
        etag = str(response.get("ETag") or "")
        if not etag:
            raise ObjectStorageError("object storage did not report the stored version")
        return etag


class CanonicalPublisher(CanonicalReader):
    """Read and write access to canonical objects (Worker)."""

    def __init__(self, access: R2Access | None = None, *, client: Any | None = None) -> None:
        super().__init__(access or canonical_publisher_access(), client=client)

    def write(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        expected_etag: str | None,
    ) -> str:
        """Create or replace one object and return its new version.

        ``expected_etag=None`` creates the object and fails if it already exists;
        otherwise the object is replaced only while it still has that version.
        Either way a concurrent writer is detected instead of overwritten.
        """
        _require_canonical_key(key)
        if not isinstance(body, bytes) or not body:
            raise ObjectStorageError("canonical object must be non-empty bytes")
        arguments: dict[str, Any] = {
            "Bucket": self._access.bucket,
            "Key": key,
            "Body": body,
            "ContentLength": len(body),
            "ContentType": content_type,
            "Metadata": {"sha256": hashlib.sha256(body).hexdigest()},
        }
        if expected_etag is None:
            arguments["IfNoneMatch"] = "*"
        else:
            arguments["IfMatch"] = expected_etag
        try:
            response = self._client.put_object(**arguments)
        except ClientError as exc:
            if _error_code(exc) in _PRECONDITION:
                raise ObjectChangedError("canonical object changed since it was read") from None
            raise ObjectStorageError("object storage write failed") from None
        except BotoCoreError:
            raise ObjectStorageError("object storage write failed") from None
        etag = str(response.get("ETag") or "")
        if not etag:
            raise ObjectStorageError("object storage did not report the written version")
        return etag

    def delete(self, key: str) -> None:
        """Remove one object; deleting an absent object is not an error."""
        _require_canonical_key(key)
        try:
            self._client.delete_object(Bucket=self._access.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND:
                return
            raise ObjectStorageError("object storage delete failed") from None
        except BotoCoreError:
            raise ObjectStorageError("object storage delete failed") from None


def _require_canonical_key(key: str) -> None:
    if not isinstance(key, str) or _CANONICAL_KEY.fullmatch(key) is None or not _valid_key(key):
        raise ObjectStorageError("canonical object key is not safe")
