FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY dashboard ./dashboard
# Editable install keeps the repository layout, so the dashboard file is found at runtime.
RUN pip install -e . && useradd --create-home --uid 10001 salesdesk && mkdir /data && chown salesdesk /data
USER salesdesk
ENV WEB_HOST=0.0.0.0 DB_PATH=/data/salesdesk.db BACKUP_DIR=/data/backups
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["salesdesk"]
