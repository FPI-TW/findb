# RAG Supply Contract

FinDB does not own semantic search, embeddings, vector indexes, or RAG prompt/runtime logic.
RAG is a downstream consumer of canonical financial data.

## Boundary

FinDB provides:

- Serve API read endpoints for canonical entities and time series.
- DB-backed Serve API keys with a dedicated tier for RAG clients.
- Optional direct reads from an RDS read replica when batch extraction volume is too high for HTTP.

FinDB does not provide:

- Embedding generation.
- Vector storage such as pgvector collections.
- RAG-specific ranking, chunking, prompts, or chat endpoints.
- Writes from the RAG service back into canonical tables.

## Recommended Consumption Path

Default path:

1. RAG service receives its own Serve API key with `tier="rag"` and `scopes=["serve"]`.
2. It extracts instruments, macro series, EOD summaries, bond data, and other canonical data through Serve API.
3. It builds embeddings and vector indexes in its own repo/service.
4. It links answer citations back to FinDB identifiers such as `instrument_id`, `series_id`, and trade dates.

High-volume path:

1. RAG service gets read-only credentials to an RDS read replica.
2. It reads canonical tables only.
3. It never writes into FinDB tables and never bypasses FinDB migrations.

## API Changes For RAG

If RAG needs better extraction support, treat it as a normal Serve API feature. Examples:

- `updated_since` filters for incremental extraction.
- bulk export jobs that return CSV/Parquet through pre-signed URLs.
- additional read-only projection fields.

These changes belong in FinDB only when they are generally useful read-path features. RAG-only storage,
embedding, or retrieval logic remains out of scope.
