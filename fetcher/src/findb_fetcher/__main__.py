"""Safe container entry point that only validates local runtime readiness."""

from __future__ import annotations

from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry


def main() -> None:
    config = FetcherConfig.from_env()
    ContractRegistry(config.contracts_dir)
    print("Fetcher configuration and contracts are valid")


if __name__ == "__main__":
    main()
