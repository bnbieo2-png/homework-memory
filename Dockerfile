FROM python:3.12-slim@sha256:9e869b0816f5537709825b49e62dc86d1c2691eff19b05c1d4dc3a07992cc052

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOMEWORK_NOTEBOOK_DATA_DIR=/data \
    HOMEWORK_NOTEBOOK_PORT=18765

WORKDIR /app

RUN groupadd --gid 10001 notebook \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin notebook

COPY requirements-web.txt ./
RUN pip install --no-cache-dir --requirement requirements-web.txt

COPY --chown=10001:10001 web_app.py learning_enrichment.py ./

USER 10001:10001
EXPOSE 18765

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18765/healthz', timeout=2).read()"]

CMD ["python", "web_app.py", "--host", "0.0.0.0"]
