"""R2 access by role: bounded, checksum-proven reads and version-checked writes."""

from __future__ import annotations

import hashlib
import traceback

import pytest
from botocore.exceptions import EndpointConnectionError

from app.config import Settings
from app.services import object_storage as storage
from tests.object_storage_fake import SECRET, FakeR2
from tests.object_storage_fake import client_error as _client_error

ACCOUNT = "a" * 32
RAW = storage.R2Access(ACCOUNT, "findb-test-raw", "reader-id", SECRET)
CANONICAL = storage.R2Access(ACCOUNT, "findb-test-canonical", "publisher-id", SECRET)
BODY = b"provider bytes"
DIGEST = hashlib.sha256(BODY).hexdigest()
KEY = "finlab/tw_equity_bar_1m/0123456789abcdef/" + DIGEST + ".parquet"
REF = f"r2://{ACCOUNT}/findb-test-raw/{KEY}"


@pytest.fixture
def r2() -> FakeR2:
    fake = FakeR2()
    fake.objects[("findb-test-raw", KEY)] = BODY
    return fake


def test_a_role_without_its_credential_cannot_build_access() -> None:
    worker = Settings(
        CLOUDFLARE_R2_ACCOUNT_ID=ACCOUNT,
        CLOUDFLARE_R2_RAW_BUCKET="findb-test-raw",
        CLOUDFLARE_R2_RAW_READER_ACCESS_KEY_ID="reader-id",
        CLOUDFLARE_R2_RAW_READER_SECRET_ACCESS_KEY=SECRET,
        CLOUDFLARE_R2_CANONICAL_BUCKET="findb-test-canonical",
        CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID="publisher-id",
        CLOUDFLARE_R2_CANONICAL_PUBLISHER_SECRET_ACCESS_KEY=SECRET,
    )

    assert storage.raw_reader_access(worker).bucket == "findb-test-raw"
    assert storage.canonical_publisher_access(worker).access_key_id == "publisher-id"
    # Worker was not given the Serve credential, and gets no substitute for it.
    with pytest.raises(storage.ObjectStorageConfigError):
        storage.canonical_reader_access(worker)
    with pytest.raises(storage.ObjectStorageConfigError):
        storage.raw_reader_access(Settings())


def test_access_never_shows_its_credential() -> None:
    assert SECRET not in repr(RAW) and "reader-id" not in repr(RAW)


@pytest.mark.parametrize(
    "ref",
    [
        f"https://{ACCOUNT}/findb-test-raw/{KEY}",
        f"r2://user:pw@{ACCOUNT}/findb-test-raw/{KEY}",
        f"r2://{ACCOUNT}:443/findb-test-raw/{KEY}",
        f"r2://{ACCOUNT}/findb-test-raw/{KEY}?versionId=1",
        f"r2://{ACCOUNT}/findb-test-raw/finlab/../{DIGEST}.parquet",
        f"r2://{ACCOUNT}/findb-test-raw/finlab//{DIGEST}.parquet",
        f"r2://{ACCOUNT}/findb-test-raw/finlab/%2e%2e/{DIGEST}.parquet",
        f"r2://{ACCOUNT}/findb-test-raw",
        "r2://not-an-account/findb-test-raw/key",
        "r2://" + ACCOUNT + "/findb-test-raw/" + "k" * 2048,
    ],
)
def test_only_a_bounded_credential_free_reference_is_parsed(ref) -> None:
    with pytest.raises(storage.ObjectIntegrityError):
        storage.parse_raw_ref(ref)


def test_a_raw_object_is_returned_only_when_it_matches_its_reference(r2) -> None:
    reader = storage.RawObjectReader(RAW, client=r2)

    assert reader.read(REF, sha256=DIGEST, max_bytes=len(BODY)) == BODY
    assert [name for name, _ in r2.calls] == ["get"]

    with pytest.raises(storage.ObjectIntegrityError):
        reader.read(REF, sha256="0" * 64, max_bytes=len(BODY))
    with pytest.raises(storage.ObjectIntegrityError):
        reader.read(REF, sha256=DIGEST, max_bytes=len(BODY) - 1)


@pytest.mark.parametrize(
    "ref",
    [
        f"r2://{ACCOUNT}/findb-other-raw/{KEY}",
        f"r2://{'b' * 32}/findb-test-raw/{KEY}",
        f"r2://{ACCOUNT}/findb-test-canonical/{KEY}",
    ],
)
def test_a_reference_outside_this_environment_is_refused_before_any_request(r2, ref) -> None:
    with pytest.raises(storage.ObjectIntegrityError):
        storage.RawObjectReader(RAW, client=r2).read(ref, sha256=DIGEST, max_bytes=1024)

    assert r2.calls == []


@pytest.mark.parametrize("declared", [len(BODY) - 1, len(BODY) + 1])
def test_a_body_that_differs_from_its_stored_length_is_refused(r2, declared) -> None:
    r2.declared_length = declared

    with pytest.raises(storage.ObjectIntegrityError):
        storage.RawObjectReader(RAW, client=r2).read(REF, sha256=DIGEST, max_bytes=1024)


def test_a_missing_object_and_a_failed_read_are_told_apart_without_provider_words(r2) -> None:
    reader = storage.RawObjectReader(RAW, client=r2)
    missing = REF.replace(DIGEST, "f" * 64)

    with pytest.raises(storage.ObjectNotFoundError):
        reader.read(missing, sha256="f" * 64, max_bytes=1024)

    for failure in (
        _client_error("AccessDenied", 403),
        EndpointConnectionError(endpoint_url="https://" + SECRET),
    ):
        r2.failure = failure
        with pytest.raises(storage.ObjectStorageError) as raised:
            reader.read(REF, sha256=DIGEST, max_bytes=1024)
        assert not isinstance(raised.value, storage.ObjectNotFoundError)
        assert SECRET not in "".join(traceback.format_exception(raised.value))


def test_a_canonical_object_is_created_once_and_replaced_only_at_its_known_version() -> None:
    r2 = FakeR2()
    publisher = storage.CanonicalPublisher(CANONICAL, client=r2)
    key = "intraday/v1/tw_equity_bar_1m/symbol=2330/year=2026/month=11/day=13/part.parquet"

    first = publisher.write(
        key, b"day one", content_type="application/x-parquet", expected_etag=None
    )
    assert publisher.read(key, max_bytes=64) == storage.StoredObject(b"day one", first)
    # A second creator, or a replacement based on a stale read, is refused.
    with pytest.raises(storage.ObjectChangedError):
        publisher.write(key, b"other", content_type="application/x-parquet", expected_etag=None)
    second = publisher.write(
        key, b"day one, republished", content_type="application/x-parquet", expected_etag=first
    )
    with pytest.raises(storage.ObjectChangedError):
        publisher.write(key, b"late", content_type="application/x-parquet", expected_etag=first)

    assert second != first
    assert r2.objects[("findb-test-canonical", key)] == b"day one, republished"
    put = next(arguments for name, arguments in r2.calls if name == "put")
    assert put["Metadata"] == {"sha256": hashlib.sha256(b"day one").hexdigest()}

    # The current version is readable without the bytes, e.g. to adopt an object
    # a crashed writer left behind.
    assert publisher.version(key) == second
    publisher.delete(key)
    publisher.delete(key)
    assert publisher.version(key) is None
    with pytest.raises(storage.ObjectNotFoundError):
        publisher.read(key, max_bytes=64)


@pytest.mark.parametrize(
    "key",
    ["", "/leading", "a//b", "a/../b", "a/./b", "a/b?x=1", "a b", "a%2Fb", "a\\b", "k" * 1025],
)
def test_unsafe_canonical_keys_never_reach_the_store(key) -> None:
    r2 = FakeR2()
    publisher = storage.CanonicalPublisher(CANONICAL, client=r2)

    with pytest.raises(storage.ObjectStorageError):
        publisher.write(key, b"x", content_type="application/x-parquet", expected_etag=None)
    with pytest.raises(storage.ObjectStorageError):
        publisher.read(key, max_bytes=1)
    with pytest.raises(storage.ObjectStorageError):
        publisher.version(key)
    with pytest.raises(storage.ObjectStorageError):
        publisher.delete(key)

    assert r2.calls == []


def test_an_empty_canonical_object_is_never_written() -> None:
    r2 = FakeR2()

    with pytest.raises(storage.ObjectStorageError):
        storage.CanonicalPublisher(CANONICAL, client=r2).write(
            "intraday/v1/x", b"", content_type="application/x-parquet", expected_etag=None
        )

    assert r2.calls == []


def test_the_serve_role_can_only_read() -> None:
    r2 = FakeR2()
    r2.objects[("findb-test-canonical", "intraday/v1/x")] = b"canonical"
    reader = storage.CanonicalReader(CANONICAL, client=r2)

    assert reader.read("intraday/v1/x", max_bytes=64).body == b"canonical"
    assert not hasattr(reader, "write") and not hasattr(reader, "delete")


def test_a_range_read_returns_only_bytes_of_the_named_version() -> None:
    r2 = FakeR2()
    key = "intraday/v1/x/symbol=2330/year=2026/month=11/part.parquet"
    publisher = storage.CanonicalPublisher(CANONICAL, client=r2)
    etag = publisher.write(key, b"0123456789", content_type="x", expected_etag=None)
    reader = storage.CanonicalReader(CANONICAL, client=r2)

    assert reader.read_range(key, etag=etag, byte_range="bytes=2-4", max_bytes=8) == (b"234", 10)
    # A suffix longer than the object returns all of it.
    assert reader.read_range(key, etag=etag, byte_range="bytes=-64", max_bytes=64) == (
        b"0123456789",
        10,
    )
    with pytest.raises(storage.ObjectIntegrityError):
        reader.read_range(key, etag=etag, byte_range="bytes=-64", max_bytes=4)
    # Once the object is replaced, the old version's ranges are refused, not mixed in.
    publisher.write(key, b"replaced!!", content_type="x", expected_etag=etag)
    with pytest.raises(storage.ObjectChangedError):
        reader.read_range(key, etag=etag, byte_range="bytes=0-1", max_bytes=8)
    with pytest.raises(storage.ObjectNotFoundError):
        reader.read_range(key + "x", etag=etag, byte_range="bytes=0-1", max_bytes=8)


def test_a_presigned_url_names_one_key_and_a_bounded_lifetime() -> None:
    r2 = FakeR2()
    reader = storage.CanonicalReader(CANONICAL, client=r2)

    url = reader.presign_get("intraday/v1/x", expires_in=900)

    assert url.startswith("https://")
    assert r2.calls == [
        (
            "presign",
            {
                "operation": "get_object",
                "Bucket": "findb-test-canonical",
                "Key": "intraday/v1/x",
                "ExpiresIn": 900,
            },
        )
    ]
    for lifetime in (0, 3601):
        with pytest.raises(storage.ObjectStorageError):
            reader.presign_get("intraday/v1/x", expires_in=lifetime)
    with pytest.raises(storage.ObjectStorageError):
        reader.presign_get("../x", expires_in=900)


@pytest.mark.parametrize("byte_range", ["", "bytes=0-", "bytes=-0", "bytes=1-2,4-5", "0-1"])
def test_only_bounded_single_ranges_are_requested(byte_range) -> None:
    r2 = FakeR2()
    reader = storage.CanonicalReader(CANONICAL, client=r2)
    with pytest.raises(storage.ObjectStorageError):
        reader.read_range("intraday/v1/x", etag='"v"', byte_range=byte_range, max_bytes=8)
    assert r2.calls == []


def test_the_access_check_reports_every_step_and_leaves_nothing_behind() -> None:
    from scripts.check_r2_access import canonical_cycle, passed, write_refused

    r2 = FakeR2()
    key = "acceptance/r2-access-probe/0190-probe"

    result = canonical_cycle(storage.CanonicalPublisher(CANONICAL, client=r2), key, keep=False)

    assert result == {
        "key": key,
        "create": "ok",
        "create_again": "refused",
        "replace_at_known_version": "ok",
        "replace_at_stale_version": "refused",
        "read_back": "ok",
        "delete": "ok",
    }
    assert passed(result) and r2.objects == {}
    # A store that ignores the version condition must fail the check.
    assert not passed({**result, "replace_at_stale_version": "NOT REFUSED"})

    r2.failure = _client_error("AccessDenied", 403)
    assert write_refused(r2, "findb-test-raw", key) == "refused"
    r2.failure = None
    assert write_refused(r2, "findb-test-raw", key).startswith("WRITTEN")
    assert not passed({"raw_reader_write": write_refused(r2, "findb-test-raw", key + "2")})
