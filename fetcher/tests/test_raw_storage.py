from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from typing import Any

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from findb_fetcher.raw_storage import (
    R2RawPayloadStore,
    RawObject,
    RawStorageConfig,
    RawStorageConfigError,
    RawStorageUploadError,
    attach_raw_object,
    attach_raw_provenance,
)

RAW_BYTES = b'{\n  "status": "ok", "values": []\n}\n'
ACCOUNT_ID = "a" * 32
ACCESS_KEY_ID = "r2-access-key"
SECRET_ACCESS_KEY = "r2-secret-key"


class FakeR2:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure


def _config(**overrides: Any) -> RawStorageConfig:
    values: dict[str, Any] = {
        "account_id": ACCOUNT_ID,
        "bucket": "findb-fetcher-raw-prod",
        "access_key_id": ACCESS_KEY_ID,
        "secret_access_key": SECRET_ACCESS_KEY,
    }
    values.update(overrides)
    return RawStorageConfig(**values)


def _request() -> dict[str, Any]:
    return {
        "dataset_key": "us_equity_eod",
        "request_key": "twelve_data:us_equity_eod:" + "a" * 32,
        "idempotency_key": "twelve_data:" + "a" * 64,
        "payload": {"batch": {}, "data": []},
    }


def test_r2_store_uploads_exact_bytes_with_deterministic_metadata() -> None:
    client = FakeR2()
    store = R2RawPayloadStore(
        _config(prefix="/immutable/provider/"),
        client=client,
    )

    first = store.persist(RAW_BYTES, dataset_key="us_equity_eod", source_symbol="AAPL")
    second = store.persist(RAW_BYTES, dataset_key="us_equity_eod", source_symbol="AAPL")

    digest_bytes = hashlib.sha256(RAW_BYTES).digest()
    digest = digest_bytes.hex()
    assert first == second
    assert first.sha256 == digest
    assert first.size_bytes == len(RAW_BYTES)
    assert first.ref.startswith(f"r2://{ACCOUNT_ID}/findb-fetcher-raw-prod/immutable/provider/")
    assert all(token not in first.ref for token in ("?", "#", "@"))
    assert client.calls[0]["Body"] == RAW_BYTES
    assert client.calls[0]["ContentLength"] == len(RAW_BYTES)
    assert client.calls[0]["ChecksumSHA256"] == base64.b64encode(digest_bytes).decode("ascii")
    assert client.calls[0]["Metadata"] == {
        "sha256": digest,
        "provider": "twelve_data",
        "dataset": "us_equity_eod",
        "source-symbol": "AAPL",
    }
    assert "ServerSideEncryption" not in client.calls[0]
    assert "SSEKMSKeyId" not in client.calls[0]
    assert client.calls[0]["Key"] == client.calls[1]["Key"]


def test_r2_store_builds_only_the_account_scoped_cloudflare_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_client(service_name: str, **kwargs: Any) -> FakeR2:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return FakeR2()

    monkeypatch.setattr("boto3.client", fake_client)
    config = _config(session_token="short-lived-token")

    R2RawPayloadStore(config)

    assert captured["service_name"] == "s3"
    assert captured["endpoint_url"] == (f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com")
    assert captured["aws_access_key_id"] == ACCESS_KEY_ID
    assert captured["aws_secret_access_key"] == SECRET_ACCESS_KEY
    assert captured["aws_session_token"] == "short-lived-token"
    assert captured["region_name"] == "auto"
    assert ACCESS_KEY_ID not in repr(config)
    assert SECRET_ACCESS_KEY not in repr(config)


def test_r2_store_key_is_collision_safe_for_content_and_symbol() -> None:
    client = FakeR2()
    store = R2RawPayloadStore(_config(), client=client)

    first = store.persist(RAW_BYTES, dataset_key="us_equity_eod", source_symbol="AAPL")
    changed_content = store.persist(
        RAW_BYTES + b" ", dataset_key="us_equity_eod", source_symbol="AAPL"
    )
    changed_symbol = store.persist(RAW_BYTES, dataset_key="us_equity_eod", source_symbol="MSFT")

    assert len({first.ref, changed_content.ref, changed_symbol.ref}) == 3


def test_r2_store_uses_explicit_provider_for_key_and_metadata() -> None:
    client = FakeR2()
    store = R2RawPayloadStore(_config(), client=client)

    raw_object = store.persist(
        RAW_BYTES,
        dataset_key="tw_equity_eod",
        source_symbol="dataset_bundle",
        provider="finlab",
    )

    assert "/raw/finlab/tw_equity_eod/" in raw_object.ref
    assert client.calls[0]["Metadata"]["provider"] == "finlab"
    assert client.calls[0]["Metadata"]["dataset"] == "tw_equity_eod"


def test_r2_store_enforces_object_bound_and_wraps_client_failures() -> None:
    bounded = R2RawPayloadStore(
        _config(max_object_bytes=4),
        client=FakeR2(),
    )
    with pytest.raises(RawStorageUploadError, match="size limit"):
        bounded.persist(b"12345", dataset_key="us_equity_eod", source_symbol="AAPL")

    failed = R2RawPayloadStore(
        _config(),
        client=FakeR2(RuntimeError("secret provider detail")),
    )
    with pytest.raises(RawStorageUploadError, match="upload failed"):
        failed.persist(RAW_BYTES, dataset_key="us_equity_eod", source_symbol="AAPL")


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (503, "ServiceUnavailable", True),
        (429, "SlowDown", True),
        (403, "AccessDenied", False),
        (404, "NoSuchBucket", False),
    ],
)
def test_r2_store_classifies_retryable_failures_safely(
    status: int,
    code: str,
    retryable: bool,
) -> None:
    failure = ClientError(
        {
            "Error": {"Code": code, "Message": "provider-secret-detail"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )
    store = R2RawPayloadStore(
        _config(),
        client=FakeR2(failure),
    )

    with pytest.raises(RawStorageUploadError) as error:
        store.persist(RAW_BYTES, dataset_key="us_equity_eod", source_symbol="AAPL")

    assert error.value.retryable is retryable
    assert "provider-secret-detail" not in str(error.value)


def test_r2_store_treats_missing_runtime_credentials_as_terminal() -> None:
    store = R2RawPayloadStore(
        _config(),
        client=FakeR2(NoCredentialsError()),
    )

    with pytest.raises(RawStorageUploadError) as error:
        store.persist(RAW_BYTES, dataset_key="us_equity_eod", source_symbol="AAPL")

    assert not error.value.retryable


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": "not-an-account"},
        {"bucket": "192.168.1.1"},
        {"bucket": "Invalid_Bucket"},
        {"bucket": "findb-fetcher-raw", "prefix": "raw/../private"},
        {"access_key_id": ""},
        {"secret_access_key": ""},
    ],
)
def test_raw_storage_config_rejects_unsafe_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(RawStorageConfigError):
        _config(**kwargs)


def test_raw_storage_config_reads_r2_credentials_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_R2_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("CLOUDFLARE_R2_BUCKET", "findb-fetcher-raw-prod")
    monkeypatch.setenv("CLOUDFLARE_R2_ACCESS_KEY_ID", ACCESS_KEY_ID)
    monkeypatch.setenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", SECRET_ACCESS_KEY)
    monkeypatch.setenv("CLOUDFLARE_R2_SESSION_TOKEN", "short-lived-token")
    monkeypatch.delenv("CLOUDFLARE_R2_PREFIX", raising=False)

    config = RawStorageConfig.from_env()

    assert config.account_id == ACCOUNT_ID
    assert config.bucket == "findb-fetcher-raw-prod"
    assert config.access_key_id == ACCESS_KEY_ID
    assert config.secret_access_key == SECRET_ACCESS_KEY
    assert config.session_token == "short-lived-token"
    assert config.prefix == "raw"


def test_attach_provenance_is_all_or_none_and_changes_request_identity() -> None:
    digest = hashlib.sha256(RAW_BYTES).hexdigest()
    raw_object = RawObject(
        ref=f"r2://{ACCOUNT_ID}/findb-fetcher-raw/raw/{digest}.json",
        sha256=digest,
        size_bytes=len(RAW_BYTES),
    )
    first = _request()
    same = deepcopy(first)
    changed = deepcopy(first)

    attach_raw_object(first, raw_object)
    attach_raw_object(same, raw_object)
    attach_raw_object(
        changed,
        RawObject(
            ref=f"r2://{ACCOUNT_ID}/findb-fetcher-raw/raw/{'b' * 64}.json",
            sha256="b" * 64,
            size_bytes=1,
        ),
    )

    assert first["payload"]["batch"] == {
        "source_raw_ref": raw_object.ref,
        "source_raw_sha256": digest,
    }
    assert first["idempotency_key"] == same["idempotency_key"]
    assert first["request_key"] == same["request_key"]
    assert first["idempotency_key"] != changed["idempotency_key"]
    preserved = _request()
    preserved.update(
        dataset_key="tw_equity_minute",
        schema_id="market_minute",
        schema_version=1,
        request_key="mmr:" + "c" * 64,
        idempotency_key="mms:" + "c" * 64,
    )
    original_identity = (preserved["request_key"], preserved["idempotency_key"])
    attach_raw_object(preserved, raw_object, preserve_identity=True)
    assert (preserved["request_key"], preserved["idempotency_key"]) == original_identity
    assert preserved["payload"]["batch"]["source_raw_ref"] == raw_object.ref
    with pytest.raises(RawStorageUploadError, match="supplied together"):
        attach_raw_provenance(_request(), source_raw_ref=raw_object.ref, source_raw_sha256=None)
    with pytest.raises(RawStorageUploadError, match="supplied together"):
        attach_raw_provenance(_request(), source_raw_ref=None, source_raw_sha256=digest)


def test_attach_provenance_uses_request_source_without_partial_mutation() -> None:
    digest = hashlib.sha256(RAW_BYTES).hexdigest()
    request = _request()
    request["source"] = "finlab"

    attach_raw_provenance(
        request,
        source_raw_ref=f"r2://{ACCOUNT_ID}/findb-fetcher-raw/raw/{digest}.json",
        source_raw_sha256=digest,
    )

    assert request["request_key"].startswith("finlab:us_equity_eod:")
    assert request["idempotency_key"].startswith("finlab:")

    invalid = _request()
    invalid["source"] = "FinLab"
    with pytest.raises(RawStorageUploadError, match="provider identity"):
        attach_raw_provenance(
            invalid,
            source_raw_ref=f"r2://{ACCOUNT_ID}/findb-fetcher-raw/raw/{digest}.json",
            source_raw_sha256=digest,
        )
    assert invalid["payload"]["batch"] == {}


@pytest.mark.parametrize(
    "ref",
    [
        f"r2://user@{ACCOUNT_ID}/findb-fetcher-raw/raw/a.json",
        f"r2://{ACCOUNT_ID}",
        f"r2://{ACCOUNT_ID}/findb-fetcher-raw/raw/../private.json",
        f"r2://{ACCOUNT_ID}/findb-fetcher-raw/raw/a.json?credential=secret",
        "r2://not-an-account/findb-fetcher-raw/raw/a.json",
        "https://findb-fetcher-raw/raw/a.json",
    ],
)
def test_attach_provenance_rejects_unsafe_r2_references(ref: str) -> None:
    with pytest.raises(RawStorageUploadError, match="bounded R2 URI"):
        attach_raw_provenance(
            _request(),
            source_raw_ref=ref,
            source_raw_sha256="a" * 64,
        )
