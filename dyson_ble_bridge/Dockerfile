ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.20
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Installed from the release tarball: no git needed inside the add-on image
COPY requirements.txt /requirements.txt
RUN pip3 install --no-cache-dir -r /requirements.txt

COPY run.sh /run.sh
RUN chmod +x /run.sh

LABEL \
  io.hass.version="0.1.2" \
  io.hass.type="addon" \
  io.hass.arch="aarch64|amd64|armv7|armhf" \
  org.opencontainers.image.source="https://github.com/andreanjos/dyson-ble-bridge" \
  org.opencontainers.image.licenses="MIT"

CMD ["/run.sh"]
