# Minimal runtime for the Mudah Telegram alert service.
#
# The service and its finder dependency are pure standard-library Python, so
# there is nothing to pip install and no third-party dependency in this image.
# Only the two modules the service imports are copied, at the relative layout
# the finder path resolution expects (/app/mudah-alerts and /app/mudah-finder).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root runtime user with a writable persistent state directory.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app /data \
    && chown -R appuser:appuser /app /data

WORKDIR /app

COPY --chown=appuser:appuser mudah-finder/mudah.py /app/mudah-finder/mudah.py
COPY --chown=appuser:appuser mudah-alerts/mudah_alerts.py /app/mudah-alerts/mudah_alerts.py

USER appuser

# SQLite state lives here. Mount a volume to persist it across restarts.
VOLUME ["/data"]

# Credentials and the automation permission flag come from the environment at
# run time; nothing sensitive is baked into the image. The config is expected
# to be mounted read-only at /config/config.json.
ENTRYPOINT ["python", "/app/mudah-alerts/mudah_alerts.py"]
CMD ["--config", "/config/config.json"]
