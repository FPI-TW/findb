from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_historical_worker_starts_inside_provider_secret_wrapper() -> None:
    helper = (ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    deploy = (ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
    marker = "findb-fetch-historical-backfill --provider"
    assert marker in helper
    assert '"${runtime_env_args[@]}"' in helper
    assert '"$image" findb-fetch-historical-backfill' in helper
    assert "FETCHER_HISTORICAL_STATE_DIR=$historical_state_dir" in helper
    # The outer script deliberately has no source/provider secrets. Starting
    # containers there would make Docker receive unset --env names.
    assert marker not in deploy
    assert "start_historical_worker" not in deploy
