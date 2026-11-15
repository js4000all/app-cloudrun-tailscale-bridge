#!/bin/sh
set -eu

: "${TS_AUTHKEY:?TS_AUTHKEY must be supplied from Secret Manager}"
: "${TS_HOSTNAME:=cloudrun-tailscale-bridge}"
: "${TS_SOCKET_PATH:=/tmp/tailscaled.sock}"
: "${PORT:=8080}"

tailscaled \
  --tun=userspace-networking \
  --state=mem: \
  --socket="$TS_SOCKET_PATH" \
  --socks5-server=127.0.0.1:1055 &
TAILSCALED_PID=$!

cleanup() {
  # The node is ephemeral; avoid leaving the child daemon running.
  kill "$TAILSCALED_PID" 2>/dev/null || true
  wait "$TAILSCALED_PID" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for the daemon's local API socket, rather than racing tailscale up.
ready=0
i=0
while [ "$i" -lt 30 ]; do
  if ! kill -0 "$TAILSCALED_PID" 2>/dev/null; then
    echo "tailscaled exited before becoming ready" >&2
    exit 1
  fi
  if tailscale --socket="$TS_SOCKET_PATH" status --json >/dev/null 2>&1; then
    ready=1
    break
  fi
  i=$((i + 1))
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "tailscaled did not become ready" >&2
  exit 1
fi

# Never log the auth key or enable shell tracing.
tailscale --socket="$TS_SOCKET_PATH" up \
  --auth-key="$TS_AUTHKEY" \
  --hostname="$TS_HOSTNAME"

# Don't keep the secret in the Gunicorn process environment.
unset TS_AUTHKEY

if [ "$(tailscale --socket="$TS_SOCKET_PATH" status --json | python -c 'import json,sys; print(json.load(sys.stdin).get("BackendState", ""))')" != "Running" ]; then
  echo "Tailscale is not Running after authentication" >&2
  exit 1
fi

echo "Tailscale connected; starting HTTP server"
gunicorn --bind "0.0.0.0:$PORT" --workers 1 --threads 4 src.main:app &
APP_PID=$!

# Reap and stop both child processes on container termination.
terminate() {
  kill "$APP_PID" 2>/dev/null || true
  cleanup
}
trap terminate INT TERM
wait "$APP_PID"
