# syntax=docker/dockerfile:1.7

# ---------- build stage: install pinned dependencies into a venv ----------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install --require-virtualenv -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --require-virtualenv --no-deps .

# ---------- runtime stage ----------
FROM python:3.13-slim

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="mealie-mcp" \
      org.opencontainers.image.description="MCP server connecting ChatGPT to a self-hosted Mealie recipe manager" \
      org.opencontainers.image.source="https://github.com/Crashandburnds/mealie-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8000

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown app:app /data && chmod 700 /data

COPY --from=builder /opt/venv /opt/venv

USER app
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["python", "-m", "mealie_mcp"]
