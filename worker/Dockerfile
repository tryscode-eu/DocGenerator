FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOCUMENT_OUTPUT_DIR=/data/documents

WORKDIR /app

COPY requirements.lock pyproject.toml README.md ./
COPY doc_worker ./doc_worker

RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin worker \
    && mkdir -p /data/documents \
    && chown -R 10001:10001 /app /data/documents

USER 10001:10001
VOLUME ["/data/documents"]
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; assert urlopen('http://127.0.0.1:8080/health/live', timeout=2).status == 200"]

CMD ["python", "-m", "doc_worker.main"]
