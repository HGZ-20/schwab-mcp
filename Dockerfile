# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml README.md ./
COPY src ./src

RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python --no-cache .

FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system schwab && \
    useradd --system --gid schwab --create-home --home-dir /home/schwab schwab && \
    mkdir -p /data && chown -R schwab:schwab /data

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SCHWAB_DATA_DIR=/data \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8765

USER schwab
WORKDIR /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3)"

ENTRYPOINT ["schwab-mcp"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
