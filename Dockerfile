FROM python:3.11.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifests first for better Docker layer cache reuse
COPY pyproject.toml uv.lock ./

# Install Python dependencies with uv into /app/.venv
RUN pip install --no-cache-dir "uv==0.9.9" && \
    uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV PATH="/app/.venv/bin:${PATH}"

# Expose port
EXPOSE 8080

# Run the application
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
