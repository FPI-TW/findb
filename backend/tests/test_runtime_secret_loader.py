"""Secret-safe tests for the Phase 2 host runtime-secret loader."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOADER_PATH = REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / "load_runtime_secrets.py"
CATALOG_ROOT = LOADER_PATH.parent
RENDERER_PATH = CATALOG_ROOT / "render_serve_key.py"


def _loader() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_secret_loader", LOADER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_secret_renderer", RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog(unit: str) -> dict[str, object]:
    loaded = json.loads((CATALOG_ROOT / f"{unit}.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_catalogs_are_v2_and_use_target_derived_relative_names() -> None:
    expected = {
        "findb": {
            "runtime/configuration",
            "database/application",
            "database/migration",
            "api/admin-break-glass",
            "api/queue-health-admin",
            "api/lookup-serve",
            "api/static-cache-serve",
            "rabbitmq/runtime",
            "r2/canonical-publisher",
            "r2/canonical-reader",
        },
        "fetcher": {
            "runtime/configuration",
            "api/calendar-serve",
            "api/source/twelve-data",
            "api/source/finlab",
            "api/source/shioaji",
            "provider/twelve-data",
            "provider/finlab",
            "provider/shioaji",
            "r2/raw",
        },
    }
    for unit, names in expected.items():
        catalog = _catalog(unit)
        assert catalog["schema_version"] == 2
        assert catalog["unit"] == unit
        assert "secret_prefix" not in catalog
        configured = {
            secret["name"]
            for consumer in catalog["consumers"].values()
            for secret in consumer["secrets"]
        }
        assert configured == names
        if unit == "findb":
            assert "canary" in catalog["consumers"]
        else:
            assert "canary" not in catalog["consumers"]


def test_active_staging_catalog_has_seventeen_entries_and_no_ghcr_runtime_secret() -> None:
    configured = {
        secret["name"]
        for unit in ("findb", "fetcher")
        for consumer in _catalog(unit)["consumers"].values()
        for secret in consumer["secrets"]
        if "staging" in secret.get("targets", ["staging", "production"])
    }
    assert len(configured) == 17
    assert "registry/ghcr-pull" not in configured
    assert "GHCR_USERNAME" not in json.dumps([_catalog("findb"), _catalog("fetcher")])
    assert "GHCR_TOKEN" not in json.dumps([_catalog("findb"), _catalog("fetcher")])
    metadata = (REPO_ROOT / "infra" / "tofu" / "staging" / "secrets.tf").read_text(encoding="utf-8")
    # Retirement reduces the managed metadata collection to the active catalog;
    # the two legacy GHCR state addresses are intentionally absent from config.
    assert 'relative_name = "registry/ghcr-pull"' not in metadata
    assert 'resource "aws_secretsmanager_secret" "active_runtime"' in metadata
    assert "prevent_destroy = true" in metadata


def test_runtime_command_uses_only_bounded_instance_role_ecr_login() -> None:
    command = (CATALOG_ROOT / "runtime_secret_command.sh").read_text(encoding="utf-8")
    assert "--ecr-registry" in command
    assert '"$aws_account_id.dkr.ecr.ap-southeast-1.amazonaws.com"' in command
    assert 'aws ecr get-login-password --region "$region"' in command
    assert 'docker login --username AWS --password-stdin "$ecr_registry"' in command
    assert 'docker logout "$ecr_registry"' in command
    assert 'rm -rf -- "$docker_config"' in command
    assert "GHCR_USERNAME" not in command
    assert "GHCR_TOKEN" not in command


def test_runtime_loader_rejects_an_unapproved_region_before_secret_access() -> None:
    loader = _loader()

    with pytest.raises(loader.LoaderError, match="region_invalid"):
        loader._aws_fetcher("us-east-1")


def test_fetcher_consumer_loads_only_its_exact_allowlist() -> None:
    loader = _loader()
    catalog = _catalog("fetcher")
    payloads = {
        "findb/staging/fetcher/api/calendar-serve": {
            "FETCHER_CALENDAR_SERVE_API_KEY": "calendar-key"
        },
        "findb/staging/fetcher/api/source/twelve-data": {
            "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY": "source-key"
        },
        "findb/staging/fetcher/provider/twelve-data": {"TWELVE_DATA_API_KEY": "provider-key"},
        "findb/staging/fetcher/r2/raw": {
            "CLOUDFLARE_R2_RAW_ACCESS_KEY_ID": "r2-id",
            "CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY": "r2-secret",
        },
    }

    loaded = loader.load_consumer(catalog, "twelve-data", lambda name: json.dumps(payloads[name]))

    assert loaded == {
        "FETCHER_CALENDAR_SERVE_API_KEY": "calendar-key",
        "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY": "source-key",
        "TWELVE_DATA_API_KEY": "provider-key",
        "CLOUDFLARE_R2_RAW_ACCESS_KEY_ID": "r2-id",
        "CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY": "r2-secret",
    }
    assert "FINLAB_API_TOKEN" not in loaded
    assert "SHIOAJI_API_KEY" not in loaded


def test_production_consumer_loads_target_scoped_runtime_configuration() -> None:
    loader = _loader()
    catalog = {
        "schema_version": 2,
        "unit": "fetcher",
        "consumers": {
            "provider": {
                "secrets": [
                    {
                        "name": "runtime/configuration",
                        "targets": ["production"],
                        "required_keys": ["SHIOAJI_SIMULATION"],
                    },
                    {"name": "provider/key", "required_keys": ["TOKEN"]},
                ]
            }
        },
    }
    payloads = {
        "findb/production/fetcher/runtime/configuration": {"SHIOAJI_SIMULATION": "true"},
        "findb/production/fetcher/provider/key": {"TOKEN": "production-token"},
    }

    loaded = loader.load_consumer(
        catalog,
        "provider",
        lambda name: json.dumps(payloads[name]),
        deployment_target="production",
    )

    assert loaded == {"SHIOAJI_SIMULATION": "true", "TOKEN": "production-token"}


def test_staging_consumer_skips_production_only_runtime_configuration() -> None:
    loader = _loader()
    catalog = {
        "schema_version": 2,
        "unit": "fetcher",
        "consumers": {
            "provider": {
                "secrets": [
                    {
                        "name": "runtime/configuration",
                        "targets": ["production"],
                        "required_keys": ["SHIOAJI_SIMULATION"],
                    },
                    {"name": "provider/key", "required_keys": ["TOKEN"]},
                ]
            }
        },
    }
    fetched: list[str] = []

    def fetch(name: str) -> str:
        fetched.append(name)
        return json.dumps({"TOKEN": "staging-token"})

    loaded = loader.load_consumer(catalog, "provider", fetch)

    assert loaded == {"TOKEN": "staging-token"}
    assert fetched == ["findb/staging/fetcher/provider/key"]


def test_fetcher_finlab_smoke_consumer_is_provider_only() -> None:
    loader = _loader()
    catalog = _catalog("fetcher")
    fetched: list[str] = []

    def fetch(secret_name: str) -> str:
        fetched.append(secret_name)
        return json.dumps({"FINLAB_API_TOKEN": "finlab-token"})

    loaded = loader.load_consumer(catalog, "finlab-smoke", fetch)

    assert loaded == {"FINLAB_API_TOKEN": "finlab-token"}
    assert fetched == ["findb/staging/fetcher/provider/finlab"]
    assert not {
        "FETCHER_CALENDAR_SERVE_API_KEY",
        "FETCHER_FINLAB_SOURCE_CLIENT_KEY",
        "CLOUDFLARE_R2_RAW_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY",
    } & set(loaded)


def test_lookup_renderer_rejects_injection_and_keeps_output_contract_bounded() -> None:
    renderer = _renderer()

    rendered = renderer._render("lookup-token", "staging.example.com")

    assert '"lookup-token"' in rendered
    assert r"staging\.example\.com/dashboard/lookup" in rendered
    assert "$http_x_api_key" in rendered
    with pytest.raises(ValueError, match="lookup_key_invalid"):
        renderer._render("lookup-token\nmalicious", "staging.example.com")
    with pytest.raises(ValueError, match="public_host_invalid"):
        renderer._render("lookup-token", "staging.example.com/evil")


@pytest.mark.parametrize("unsafe", ["line\nvalue", "line\rvalue", "nul\x00value"])
def test_loader_rejects_unsafe_values_without_exposing_them(unsafe: str) -> None:
    loader = _loader()
    catalog = {
        "schema_version": 1,
        "unit": "unit",
        "secret_prefix": "findb/staging/unit/",
        "consumers": {
            "consumer": {
                "secrets": [{"name": "one", "required_keys": ["TOKEN"]}],
            }
        },
    }
    with pytest.raises(loader.LoaderError) as error:
        loader.load_consumer(catalog, "consumer", lambda _: json.dumps({"TOKEN": unsafe}))
    assert str(error.value) == "secret_value_unsafe"
    assert unsafe not in str(error.value)


def test_loader_rejects_unexpected_keys_and_reused_credentials() -> None:
    loader = _loader()
    catalog = {
        "schema_version": 1,
        "unit": "unit",
        "secret_prefix": "findb/staging/unit/",
        "consumers": {
            "unexpected": {
                "secrets": [{"name": "one", "required_keys": ["TOKEN"]}],
            },
            "reused": {
                "secrets": [
                    {"name": "one", "required_keys": ["TOKEN_A"]},
                    {"name": "two", "required_keys": ["TOKEN_B"]},
                ],
                "distinct_keys": [["TOKEN_A", "TOKEN_B"]],
            },
        },
    }
    with pytest.raises(loader.LoaderError, match="secret_schema_invalid"):
        loader.load_consumer(
            catalog,
            "unexpected",
            lambda _: json.dumps({"TOKEN": "value", "UNEXPECTED": "never-print-this"}),
        )
    with pytest.raises(loader.LoaderError, match="distinct_constraint_failed"):
        loader.load_consumer(
            catalog,
            "reused",
            lambda name: json.dumps({"TOKEN_A" if name.endswith("one") else "TOKEN_B": "same"}),
        )


def test_catalog_rejects_unit_prefix_mismatch(tmp_path: Path) -> None:
    loader = _loader()
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "unit": "findb",
                "secret_prefix": "findb/staging/fetcher/",
                "consumers": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(loader.LoaderError, match="catalog_invalid"):
        loader._catalog(catalog_path)


def test_load_consumer_rejects_invalid_target_without_secret_fetch() -> None:
    loader = _loader()
    catalog = {
        "schema_version": 2,
        "unit": "findb",
        "consumers": {
            "consumer": {
                "secrets": [{"name": "api/key", "required_keys": ["TOKEN"]}],
            }
        },
    }
    fetched = False

    def fetch(_: str) -> str:
        nonlocal fetched
        fetched = True
        return '{"TOKEN":"value"}'

    with pytest.raises(loader.LoaderError, match="catalog_invalid"):
        loader.load_consumer(catalog, "consumer", fetch, deployment_target="other")
    assert not fetched


@pytest.mark.parametrize(
    "secret",
    [
        {"name": "../api/key", "required_keys": ["TOKEN"]},
        {"name": "api//key", "required_keys": ["TOKEN"]},
        {"name": "api/key", "required_keys": ["TOKEN-NAME"]},
        {"name": "api/key", "required_keys": ["TOKEN"], "targets": []},
        {"name": "api/key", "required_keys": ["TOKEN"], "targets": ["other"]},
    ],
)
def test_loader_rejects_unsafe_catalog_identifiers(secret: dict[str, object]) -> None:
    loader = _loader()
    catalog = {
        "schema_version": 1,
        "unit": "findb",
        "secret_prefix": "findb/staging/findb/",
        "consumers": {"consumer": {"secrets": [secret]}},
    }
    with pytest.raises(loader.LoaderError, match="catalog_invalid"):
        loader.load_consumer(catalog, "consumer", lambda _: '{"TOKEN":"value"}')


def test_loader_rejects_unsafe_consumer_name() -> None:
    loader = _loader()
    with pytest.raises(loader.LoaderError, match="consumer_not_allowed"):
        loader.load_consumer(_catalog("findb"), "../compose", lambda _: "{}")


def test_env_file_is_exclusive_0600_owned_and_tmpfs_gated(tmp_path: Path) -> None:
    loader = _loader()
    run_root = tmp_path / "run"
    output = run_root / "findb" / "runtime.env"
    owner = (os.getuid(), os.getgid())

    loader.write_env_file(
        output,
        {"TOKEN": "dollar$ quote' space"},
        owner=owner,
        filesystem_type=lambda _: "tmpfs",
        run_root=run_root,
        runtime_root_owner=owner,
    )

    metadata = output.stat()
    parent_metadata = output.parent.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert (metadata.st_uid, metadata.st_gid) == owner
    assert stat.S_IMODE(parent_metadata.st_mode) == 0o700
    assert (parent_metadata.st_uid, parent_metadata.st_gid) == owner
    assert output.read_text(encoding="utf-8") == "TOKEN='dollar$ quote'\"'\"' space'\n"
    with pytest.raises(loader.LoaderError, match="output_exists"):
        loader.write_env_file(
            output,
            {"TOKEN": "replacement"},
            owner=owner,
            filesystem_type=lambda _: "tmpfs",
            run_root=run_root,
            runtime_root_owner=owner,
        )
    assert "replacement" not in output.read_text(encoding="utf-8")


def test_env_file_rejects_final_symlink_without_changing_target(tmp_path: Path) -> None:
    loader = _loader()
    run_root = tmp_path / "run"
    output_directory = run_root / "findb"
    output_directory.mkdir(mode=0o700, parents=True)
    run_root.chmod(0o700)
    target = output_directory / "target.env"
    target.write_text("ORIGINAL=value\n", encoding="utf-8")
    output = output_directory / "runtime.env"
    output.symlink_to(target)

    with pytest.raises(loader.LoaderError, match="output_exists"):
        loader.write_env_file(
            output,
            {"TOKEN": "replacement"},
            owner=(os.getuid(), os.getgid()),
            filesystem_type=lambda _: "tmpfs",
            run_root=run_root,
            runtime_root_owner=(os.getuid(), os.getgid()),
        )

    assert output.is_symlink()
    assert target.read_text(encoding="utf-8") == "ORIGINAL=value\n"


def test_env_file_rejects_parent_symlink_without_writing_outside(tmp_path: Path) -> None:
    loader = _loader()
    run_root = tmp_path / "run"
    outside = tmp_path / "outside"
    run_root.mkdir(mode=0o700)
    outside.mkdir()
    (run_root / "findb").symlink_to(outside, target_is_directory=True)

    with pytest.raises(loader.LoaderError, match="output_write_failed"):
        loader.write_env_file(
            run_root / "findb" / "runtime.env",
            {"TOKEN": "never-write"},
            owner=(os.getuid(), os.getgid()),
            filesystem_type=lambda _: "tmpfs",
            run_root=run_root,
            runtime_root_owner=(os.getuid(), os.getgid()),
        )

    assert not (outside / "runtime.env").exists()


def test_env_file_rejects_parent_inserted_during_tmpfs_check(tmp_path: Path) -> None:
    loader = _loader()
    run_root = tmp_path / "run"
    outside = tmp_path / "outside"
    run_root.mkdir(mode=0o700)
    outside.mkdir()

    checks = 0

    def insert_symlink(_: Path) -> str:
        nonlocal checks
        checks += 1
        if checks == 3:
            (run_root / "findb").symlink_to(outside, target_is_directory=True)
        return "tmpfs"

    with pytest.raises(loader.LoaderError, match="output_write_failed"):
        loader.write_env_file(
            run_root / "findb" / "runtime.env",
            {"TOKEN": "never-write"},
            owner=(os.getuid(), os.getgid()),
            filesystem_type=insert_symlink,
            run_root=run_root,
            runtime_root_owner=(os.getuid(), os.getgid()),
        )

    assert not (outside / "runtime.env").exists()


def test_check_only_removes_file_through_open_directory(tmp_path: Path) -> None:
    loader = _loader()
    run_root = tmp_path / "run"
    output = run_root / "findb" / "runtime.env"
    loader.write_env_file(
        output,
        {"TOKEN": "value"},
        owner=(os.getuid(), os.getgid()),
        filesystem_type=lambda _: "tmpfs",
        run_root=run_root,
        runtime_root_owner=(os.getuid(), os.getgid()),
        remove_after_validation=True,
    )
    assert not output.exists()


def test_env_file_rejects_nested_non_tmpfs_mount(tmp_path: Path) -> None:
    loader = _loader()
    calls = 0

    def filesystem_type(_: Path) -> str:
        nonlocal calls
        calls += 1
        return "tmpfs" if calls <= 3 else "ext4"

    output = tmp_path / "run" / "findb" / "runtime.env"
    with pytest.raises(loader.LoaderError, match="output_not_tmpfs"):
        loader.write_env_file(
            output,
            {"TOKEN": "never-write"},
            owner=(os.getuid(), os.getgid()),
            filesystem_type=filesystem_type,
            run_root=tmp_path / "run",
            runtime_root_owner=(os.getuid(), os.getgid()),
        )
    assert not output.exists()


def test_env_file_rejects_persistent_or_out_of_scope_paths(tmp_path: Path) -> None:
    loader = _loader()
    owner = (os.getuid(), os.getgid())
    run_root = tmp_path / "run"
    with pytest.raises(loader.LoaderError, match="runtime_parent_not_tmpfs"):
        loader.write_env_file(
            run_root / "unit" / "runtime.env",
            {"TOKEN": "value"},
            owner=owner,
            filesystem_type=lambda _: "ext4",
            run_root=run_root,
            runtime_root_owner=owner,
        )
    with pytest.raises(loader.LoaderError, match="output_outside_run"):
        loader.write_env_file(
            tmp_path / "persistent.env",
            {"TOKEN": "value"},
            owner=owner,
            filesystem_type=lambda _: "tmpfs",
            run_root=run_root,
        )


def test_runtime_root_bootstrap_recovers_a_missing_tmpfs_directory(tmp_path: Path) -> None:
    loader = _loader()
    run_root = tmp_path / "findb-runtime-secrets"
    owner = (os.getuid(), os.getgid())

    created = loader.ensure_runtime_root(
        run_root,
        filesystem_type=lambda _: "tmpfs",
        owner=owner,
    )

    metadata = run_root.stat()
    assert created is True
    assert stat.S_IMODE(metadata.st_mode) == 0o700
    assert (metadata.st_uid, metadata.st_gid) == owner
    assert (
        loader.ensure_runtime_root(
            run_root,
            filesystem_type=lambda _: "tmpfs",
            owner=owner,
        )
        is False
    )


@pytest.mark.parametrize("unsafe_kind", ("symlink", "non_directory", "unsafe_mode"))
def test_runtime_root_bootstrap_rejects_unsafe_existing_paths(
    tmp_path: Path, unsafe_kind: str
) -> None:
    loader = _loader()
    run_root = tmp_path / "findb-runtime-secrets"
    owner = (os.getuid(), os.getgid())
    if unsafe_kind == "symlink":
        target = tmp_path / "outside"
        target.mkdir()
        run_root.symlink_to(target, target_is_directory=True)
    elif unsafe_kind == "non_directory":
        run_root.write_text("not a directory", encoding="utf-8")
    else:
        run_root.mkdir(mode=0o755)

    expected_reason = (
        "runtime_root_metadata_invalid"
        if unsafe_kind == "unsafe_mode"
        else "runtime_root_path_invalid"
    )
    with pytest.raises(loader.LoaderError, match=expected_reason):
        loader.ensure_runtime_root(
            run_root,
            filesystem_type=lambda _: "tmpfs",
            owner=owner,
        )


def test_runtime_root_bootstrap_rejects_wrong_parent_filesystem_without_creating_path(
    tmp_path: Path,
) -> None:
    loader = _loader()
    run_root = tmp_path / "findb-runtime-secrets"

    with pytest.raises(loader.LoaderError, match="runtime_parent_not_tmpfs"):
        loader.ensure_runtime_root(run_root, filesystem_type=lambda _: "ext4")

    assert not run_root.exists()


def test_missing_aws_cli_is_a_bounded_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = _loader()

    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(loader.subprocess, "run", missing)
    with pytest.raises(loader.LoaderError) as error:
        loader._aws_fetcher("ap-southeast-1")("findb/staging/findb/database/application")
    assert str(error.value) == "aws_cli_missing"


def test_aws_cli_timeout_is_a_bounded_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = _loader()

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="aws", timeout=20, output="do-not-print")

    monkeypatch.setattr(loader.subprocess, "run", timeout)
    with pytest.raises(loader.LoaderError) as error:
        loader._aws_fetcher("ap-southeast-1")("findb/staging/findb/database/application")
    assert str(error.value) == "secret_fetch_failed"
    assert "do-not-print" not in str(error.value)


def test_cli_does_not_echo_invalid_consumer_input(tmp_path: Path) -> None:
    consumer_input = "bad SECRET-DO-NOT-PRINT\nINJECTED"
    completed = subprocess.run(
        [
            "python3",
            str(LOADER_PATH),
            "--catalog",
            str(CATALOG_ROOT / "findb.json"),
            "--consumer",
            consumer_input,
            "--region",
            "ap-southeast-1",
            "--deployment-target",
            "staging",
            "--output",
            str(tmp_path / "runtime.env"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "consumer=invalid reason=consumer_not_allowed" in completed.stderr
    assert "SECRET-DO-NOT-PRINT" not in completed.stderr
    assert "INJECTED" not in completed.stderr
