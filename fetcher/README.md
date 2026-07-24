# FinDB Fetcher

獨立的 FinDB Source API delivery client基礎套件。它只依賴repository-level
versioned contracts，不import backend，也不持有FinDB DB、RabbitMQ或Admin權限。

目前提供：

- Contract manifest、checksum與JSON Schema驗證。
- Canonical request確定性序列化。
- 保持相同body與idempotency key的bounded retry。
- 只重試transport errors、429、502、503、504。
- 安全的container readiness入口；不會自動抓取或送出資料。

尚未提供provider adapter、scheduler、checkpoint、S3或production deployment。

## 開發

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run black --check .
```

從repository root建置：

```bash
docker build -f fetcher/Dockerfile -t findb-fetcher:local .
```

## Runtime設定

| 變數 | 必填 | 預設 | 用途 |
| --- | --- | --- | --- |
| `SOURCE_API_URL` | 是 | — | FinDB Source API origin，不含 `/api/v1/source/ingest` |
| `SOURCE_CLIENT_KEY` | 是 | — | Fetcher專用DB-backed source client key |
| `FETCHER_CONTRACTS_DIR` | 否 | `/app/contracts` | Contract manifest目錄 |
| `FETCHER_REQUEST_TIMEOUT_SECONDS` | 否 | `30` | 單次HTTP timeout |
| `FETCHER_MAX_ATTEMPTS` | 否 | `3` | 包含首次呼叫的最大attempt數 |
| `FETCHER_MAX_RETRY_AFTER_SECONDS` | 否 | `30` | Retry-After與backoff上限 |

容器預設執行 `python -m findb_fetcher`。它只驗證runtime設定與contracts後退出，
不會產生delivery。Provider execution與scheduler會在後續切片加入。
