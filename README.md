# app-cloudrun-tailscale-bridge

A Python/Flask HTTP service for Google Cloud Run that joins a Tailscale tailnet in userspace mode. This iteration verifies the Tailscale connection; SSH to an edge VM and Google Apps Script integration remain separate steps.

## Health endpoint

`GET /health` queries `tailscale status --json` on every request. It checks whether the local Tailscale daemon reports `BackendState: Running`; it does **not** verify reachability of an edge VM.

Connected (HTTP 200):

```json
{"status":"ok","tailscale":{"connected":true,"backend_state":"Running"}}
```

Disconnected (HTTP 503):

```json
{"status":"error","tailscale":{"connected":false,"backend_state":"NeedsLogin"}}
```

When the daemon cannot be queried, `backend_state` is `Unavailable` and HTTP 503 is returned. The response intentionally omits Tailnet peer lists and credentials.

## Tailscale configuration

Use a reusable, ephemeral, tagged Tailscale auth key. Save its value in **Google Secret Manager**, not in this repository or container image.

- `TS_AUTHKEY` (required): Secret Manager-backed environment variable with the auth key
- `TS_HOSTNAME` (optional, default `cloudrun-tailscale-bridge`): Tailnet machine hostname
- `TS_SOCKET_PATH` (optional, default `/tmp/tailscaled.sock`): local daemon socket
- `PORT` (provided by Cloud Run, default `8080`): listening HTTP port

The container starts `tailscaled --tun=userspace-networking --state=mem:`, authenticates with `tailscale up`, verifies the backend is Running, and only then starts Gunicorn. The daemon uses an in-memory state file; each new Cloud Run instance joins as a new ephemeral device. Repeated instances may display the same requested hostname with Tailscale-assigned suffixes.

Cloud Run service identity needs `roles/secretmanager.secretAccessor` **on the specific secret**. Configure the secret as environment variable `TS_AUTHKEY` for the Cloud Run revision. Keep ingress set to **All** and **Require authentication** enabled. The intended source is the Google Apps Script identity token, not unauthenticated HTTP.

Tailscale userspace networking exposes a local SOCKS5 proxy at `127.0.0.1:1055` for future outbound connections. This iteration does not route application HTTP traffic through that proxy.

## Run tests

Requires Python 3.12+:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
docker build -t edge-gateway:local .
```

To run the application image, supply an ephemeral key via a local environment variable (avoid pasting the key into shell history):

```bash
docker run --rm -p 8080:8080 --env-file .env edge-gateway:local
curl -i http://localhost:8080/health
```

Use a private, ignored `.env` file containing `TS_AUTHKEY=...` for local testing; do not commit it. The Flask endpoint is not a public access-control boundary: locally it is unauthenticated, while Cloud Run IAM protects the deployed service.

## Deploy to Cloud Run

After configuring the service account and Secret Manager-backed `TS_AUTHKEY` environment variable for the service, build and deploy a new revision from the repository. One option for manual deployment:

```bash
gcloud run deploy edge-gateway \
  --source . \
  --region asia-northeast1 \
  --no-allow-unauthenticated \
  --ingress all \
  --min-instances 0 \
  --max-instances 1
```

Check that the secret reference and service identity are preserved in the resulting revision. Cold start includes Tailnet registration and can take longer than the original health-only service. A failed Tailnet login fails startup; check Cloud Run logs.

Verify from an authorized machine:

```bash
URL=$(gcloud run services describe edge-gateway --region asia-northeast1 --format='value(status.url)')
curl -i -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/health"
```

## Scope

This is a Tailnet registration and local daemon health probe, not a connection test to an edge VM. Edge SSH and edge information collection will be added in follow-up work.

References: https://tailscale.com/docs/install/cloud/cloudrun and https://tailscale.com/docs/concepts/userspace-networking.
