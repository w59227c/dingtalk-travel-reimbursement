FROM ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff AS uv

FROM python:3.13.15-slim-trixie@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS models

# Keep model downloads independent of application/dependency changes. This stage
# needs only the Python standard library and the committed artifact manifest.
WORKDIR /build
COPY app/ocr/model_artifacts.py app/ocr/model_manifest.json ./app/ocr/
COPY scripts/download-ocr-models.py ./scripts/download-ocr-models.py
RUN python scripts/download-ocr-models.py --output /opt/expense/models

FROM python:3.13.15-slim-trixie@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1 \
    PADDLE_PDX_CACHE_HOME=/tmp/expense/.paddlex \
    OCR_DETECTION_MODEL_DIR=/opt/expense/models/PP-OCRv6_small_det \
    OCR_RECOGNITION_MODEL_DIR=/opt/expense/models/PP-OCRv6_small_rec \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TMPDIR=/tmp/expense/.spool \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends --yes \
        libgl1 \
        libglib2.0-0t64 \
        libgomp1 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
ARG INSTALL_OCR=true
ARG TARGETARCH
RUN if [ "$INSTALL_OCR" = "true" ]; then \
        test "$TARGETARCH" = "amd64" \
            || { echo "Linux OCR images require --platform linux/amd64" >&2; exit 1; }; \
        uv sync --frozen --no-dev --no-install-project --extra ocr; \
    else \
        uv sync --frozen --no-dev --no-install-project; \
    fi

COPY app ./app
COPY --from=models --chown=0:0 /opt/expense/models /opt/expense/models
COPY licenses /usr/share/licenses/dingtalk-expense

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh ./docker-entrypoint.sh

RUN groupadd --gid 10001 expense \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin expense \
    && chmod 0755 /app/docker-entrypoint.sh \
    && mkdir -p /app/data /app/staging /tmp/expense/.spool /opt/expense/models \
    && chown expense:expense /app/data /tmp/expense /tmp/expense/.spool /app/staging \
    && chmod 0700 /app/data /app/staging /tmp/expense /tmp/expense/.spool \
    && chmod -R a-w /app/app /app/migrations /app/alembic.ini /app/docker-entrypoint.sh \
        /opt/expense/models /usr/share/licenses/dingtalk-expense

USER 10001:10001

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
