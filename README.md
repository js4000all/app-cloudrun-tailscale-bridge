# app-cloudrun-tailscale-bridge

A small HTTP service intended to run on Google Cloud Run. This initial version provides only a health endpoint; Tailscale, SSH, edge data collection, and Google Apps Script integration are planned separately.

## API

`GET /health` returns HTTP 200 and:

```json
{"status":"ok"}
```

The health endpoint is independent of downstream services.

## Run locally

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
gunicorn --bind 0.0.0.0:8080 src.main:app
```

Open another terminal:

```bash
curl -i http://localhost:8080/health
```

## Test

```bash
python -m pytest -q
docker build -t edge-gateway:local .
```

## Deploy to Cloud Run

Choose a Google Cloud project with billing enabled and Cloud Run, Cloud Build, and Artifact Registry APIs enabled. From the repository root, using the Google Cloud CLI:

```bash
gcloud run deploy edge-gateway \
  --source . \
  --region asia-northeast1 \
  --no-allow-unauthenticated \
  --ingress all \
  --min-instances 0 \
  --max-instances 1
```

The service requires IAM authentication. Grant the caller `roles/run.invoker` as appropriate. For testing from an authorized Google Cloud CLI session:

```bash
URL=$(gcloud run services describe edge-gateway --region asia-northeast1 --format='value(status.url)')
curl -i -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/health"
```

In the Cloud Run web UI, select **Require authentication** and **All** ingress. Do not enable public unauthenticated invocation just to test this endpoint.

## Scope

This repository does not yet connect to Tailscale or any edge VM. Never commit authentication keys or other credentials.
