"""Historical requests keep independent transactional deployment identities."""

from pathlib import Path


def test_historical_workers_use_same_transaction_as_profile_workers():
    root = Path(__file__).resolve().parents[2]
    helper = (root / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    coordinator = (root / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
    assert 'stable="findb-historical-$provider"' in coordinator
    section = coordinator[coordinator.index("export FETCHER_CONSUMER_PROFILE=historical") :]
    assert 'register_provider "$stable" "${stable}-previous"' in section
    assert 'findb-fetch-historical-backfill --provider "${provider//-/_}"' in section
    assert "historical:findb-fetch-historical-backfill" in helper
    assert "--label com.findb.fetcher.historical-shutdown=date-boundary-v1" in helper
    assert 'docker run -d --name "$historical_name"' not in helper
