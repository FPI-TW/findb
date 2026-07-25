FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir "uv==0.9.9"

COPY fetcher/pyproject.toml fetcher/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY fetcher/src ./src
COPY fetcher/configs ./configs
COPY contracts ./contracts
RUN uv sync --frozen --no-dev

RUN groupadd --system --gid 10001 fetcher \
    && useradd --system --uid 10001 --gid fetcher --home-dir /nonexistent fetcher \
    && mkdir -p /var/lib/findb-fetcher \
    && chown fetcher:fetcher /var/lib/findb-fetcher \
    && chown -R fetcher:fetcher /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    FETCHER_CONTRACTS_DIR=/app/contracts

VOLUME ["/var/lib/findb-fetcher"]

USER fetcher

CMD ["python", "-m", "findb_fetcher"]
