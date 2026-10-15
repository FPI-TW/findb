"""Cache ownership and SDK lock regressions without optional provider dependencies."""

import gc
import threading
import traceback
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pytest

from findb_fetcher.providers import finlab

DAY = date(2026, 10, 15)


class Frame:
    index = (DAY,)
    columns = ("2330",)
    loc = {(DAY, "2330"): 123}


class FileStorage:
    def __init__(self):
        self._cache = {}
        self.use_cache = True
        self._created = {"sentinel": 42}
        self._expiry = {"sentinel": 100}

    def clear(self):
        raise AssertionError("disk cache must never be deleted")


class CacheStorage:
    def __init__(self):
        self._cache = {"sentinel": Frame()}


def gateway(data, callback=None, login=lambda _: None):
    return finlab.FinLabSdkGateway(
        "offline-token",
        _sdk=SimpleNamespace(login=login, data=data),
        on_response_bytes=callback,
    )


def invoke(client, dataset="price:收盤價"):
    return client.fetch_dataset(dataset, target_date=DAY, symbols=("2330",))


def getter(namespace):
    # Model the SDK's real function-global storage, independent of re-exports.
    exec("def get(dataset):\n return acquire(_storage, dataset)", namespace)
    return namespace["get"]


def test_repeated_five_field_calls_release_frames_without_changing_metadata():
    storage = FileStorage()
    retained = Frame()
    references = [weakref.ref(retained)]
    storage._cache["old"] = retained
    del retained
    calls = []

    def acquire(current, dataset):
        assert current._cache == {}
        assert current.use_cache is False
        frame = Frame()
        references.append(weakref.ref(frame))
        if current.use_cache:
            current._cache[dataset] = frame
        calls.append(dataset)
        return frame

    data = SimpleNamespace(
        FileStorage=FileStorage,
        _storage=storage,
        get=getter({"_storage": storage, "acquire": acquire}),
    )
    for _ in range(3):
        for field in ("open", "high", "low", "close", "volume"):
            assert invoke(gateway(data), field).values == ((123,),)
            gc.collect()
            assert all(reference() is None for reference in references)
    assert len(calls) == 15
    assert storage._created == {"sentinel": 42}
    assert storage._expiry == {"sentinel": 100}


@pytest.mark.parametrize("replacement_type", [FileStorage, CacheStorage])
@pytest.mark.parametrize("fails", [False, True])
def test_replacement_cleans_previous_actual_and_stale_public_storage(replacement_type, fails):
    public = FileStorage()
    actual = FileStorage()
    replacement = replacement_type()
    for storage in (public, actual):
        storage._cache["old"] = Frame()
    namespace = {"_storage": actual}

    def acquire(current, dataset):
        assert current is actual
        assert public._cache == actual._cache == {}
        namespace["_storage"] = replacement
        frame = Frame()
        replacement._cache[dataset] = frame
        if fails:
            raise RuntimeError("secret-provider-context")
        return frame

    namespace["acquire"] = acquire
    data = SimpleNamespace(FileStorage=FileStorage, _storage=public, get=getter(namespace))
    if fails:
        with pytest.raises(finlab.FinLabSdkError) as raised:
            invoke(gateway(data))
        assert "secret-provider-context" not in "".join(traceback.format_exception(raised.value))
    else:
        assert invoke(gateway(data)).values == ((123,),)
    assert public._cache == actual._cache == {}
    assert not public.use_cache and not actual.use_cache
    if replacement_type is FileStorage:
        assert replacement._cache == {}
        assert not replacement.use_cache
    else:
        assert set(replacement._cache) == {"sentinel", "price:收盤價"}


@pytest.mark.parametrize("failure", ["login", "get", "bytes", "callback", "projection"])
def test_cache_is_released_on_every_failure_and_errors_remain_safe(monkeypatch, failure):
    storage = FileStorage()
    references = []

    def fail(*args, **kwargs):
        storage._cache["during-error"] = Frame()
        references.append(weakref.ref(storage._cache["during-error"]))
        raise RuntimeError("secret-provider-context")

    data = SimpleNamespace(
        FileStorage=FileStorage,
        _storage=storage,
        get=fail if failure == "get" else lambda _: Frame(),
    )
    monkeypatch.setattr(
        finlab, "_dataframe_response_bytes", fail if failure == "bytes" else lambda _: 10
    )
    if failure == "projection":
        monkeypatch.setattr(finlab, "_table_from_dataframe", fail)
    client = gateway(
        data,
        callback=fail if failure == "callback" else lambda _: None,
        login=fail if failure == "login" else lambda _: None,
    )
    with pytest.raises(finlab.FinLabSdkError) as raised:
        invoke(client)
    assert "secret-provider-context" not in "".join(traceback.format_exception(raised.value))
    assert storage._cache == {}
    # Error tracebacks can own temporary frames until released; the SDK cannot.
    del raised
    gc.collect()
    assert all(reference() is None for reference in references)


def test_projection_and_cache_cleanup_hold_sdk_lock(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    other_attempting = threading.Event()
    other_get = threading.Event()
    storage = FileStorage()
    original = finlab._table_from_dataframe

    def projection(frame, **kwargs):
        if threading.current_thread().name.startswith("first"):
            storage._cache["temporary"] = frame
            entered.set()
            assert release.wait(5)
        return original(frame, **kwargs)

    monkeypatch.setattr(finlab, "_table_from_dataframe", projection)
    first = gateway(
        SimpleNamespace(FileStorage=FileStorage, _storage=storage, get=lambda _: Frame())
    )

    def second_get(_):
        assert storage._cache == {}
        other_get.set()
        return Frame()

    second = gateway(SimpleNamespace(FileStorage=FileStorage, _storage=storage, get=second_get))

    def second_call():
        other_attempting.set()
        return invoke(second)

    with ThreadPoolExecutor(1, thread_name_prefix="first") as a, ThreadPoolExecutor(1) as b:
        first_result = a.submit(invoke, first)
        try:
            assert entered.wait(5)
            second_result = b.submit(second_call)
            assert other_attempting.wait(5)
            assert not other_get.wait(0.1)
        finally:
            release.set()
        assert first_result.result(5).values == ((123,),)
        assert second_result.result(5).values == ((123,),)
    assert other_get.is_set()


def test_custom_storage_subclasses_are_untouched():
    class CustomStorage(FileStorage):
        pass

    storage = CustomStorage()
    frame = Frame()
    storage._cache["custom"] = frame
    data = SimpleNamespace(FileStorage=FileStorage, _storage=storage, get=lambda _: frame)
    assert invoke(gateway(data)).values == ((123,),)
    assert storage._cache == {"custom": frame}
    assert storage.use_cache
