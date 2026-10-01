FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt pyproject.toml ./
COPY drop_monitor ./drop_monitor
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --no-deps .

# Unprivileged user; /app/data holds the SQLite db, log and heartbeat (mount a volume there).
RUN useradd --create-home --uid 10001 monitor && mkdir -p /app/data && chown -R monitor:monitor /app
USER monitor
VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD drop-monitor -c /app/config.yaml healthcheck || exit 1

ENTRYPOINT ["drop-monitor", "-c", "/app/config.yaml"]
CMD ["run"]
