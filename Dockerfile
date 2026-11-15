FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TS_SOCKET_PATH=/tmp/tailscaled.sock \
    TS_HOSTNAME=cloudrun-tailscale-bridge

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \\
    && apt-get install -y --no-install-recommends openssh-client \\
    && rm -rf /var/lib/apt/lists/* \\
    && pip install --no-cache-dir -r requirements.txt

# Official Tailscale binaries; no kernel TUN device is required in Cloud Run.
COPY --from=docker.io/tailscale/tailscale:stable /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=docker.io/tailscale/tailscale:stable /usr/local/bin/tailscale /usr/local/bin/tailscale

COPY src/ ./src/
COPY start.sh ./start.sh
RUN chmod 755 ./start.sh

ENV PORT=8080
CMD ["/app/start.sh"]
