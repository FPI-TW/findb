"""Retired standalone command; use the queue-owned provider runtimes."""

from findb_fetcher.legacy_cli import retired


def main(argv: list[str] | None = None) -> int:
    return retired("findb-fetch-shioaji-staging")


if __name__ == "__main__":
    raise SystemExit(main())
