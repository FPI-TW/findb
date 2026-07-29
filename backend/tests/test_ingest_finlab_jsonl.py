"""Configuration tests for the one-shot FinLab ingest utility."""

from scripts import ingest_finlab_jsonl


def test_finlab_source_key_loads_from_root_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("FINLAB_SOURCE_CLIENT_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "FINLAB_SOURCE_CLIENT_API_KEY=dotenv-db-backed-key\n", encoding="utf-8"
    )
    monkeypatch.setattr(ingest_finlab_jsonl, "REPO_ROOT", tmp_path)

    assert ingest_finlab_jsonl.load_finlab_source_client_api_key() == "dotenv-db-backed-key"


def test_finlab_source_key_prefers_exported_environment(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "FINLAB_SOURCE_CLIENT_API_KEY=dotenv-db-backed-key\n", encoding="utf-8"
    )
    monkeypatch.setattr(ingest_finlab_jsonl, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("FINLAB_SOURCE_CLIENT_API_KEY", "exported-db-backed-key")

    assert ingest_finlab_jsonl.load_finlab_source_client_api_key() == "exported-db-backed-key"
