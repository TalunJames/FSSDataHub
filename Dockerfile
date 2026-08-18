FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/TalunJames/FSSDataHub" \
      org.opencontainers.image.title="FSSDataHub" \
      org.opencontainers.image.description="Tax ledger collector for TrueNAS"

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TAX_DATABASE_DATA=/data \
    COLLECTOR_HOST=0.0.0.0 \
    COLLECTOR_PORT=8080

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Chromium for the browser pass over county sites that render rate tables in
# JavaScript. Adds roughly 500 MB; set browser_render=0 to leave it unused.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY taxdb /app/taxdb
COPY collector /app/collector
COPY bin /app/bin

RUN mkdir -p /data

EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["python", "-m", "collector"]
