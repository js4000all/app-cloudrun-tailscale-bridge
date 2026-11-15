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


## SSH command execution proof of concept

**Security boundary:** `POST /exec` accepts arbitrary remote shell commands and a target host. Protect the Cloud Run service with **Require authentication**, grant `roles/run.invoker` only to trusted callers, and do not deploy this API anonymously. Tailscale ACLs/grants and SSH policy determine which Tailnet hosts and remote OS accounts can be reached. No duplicate application-level host allowlist is maintained. Do not accept command strings from untrusted callers.

Configure the Cloud Run environment:

- `TS_SSH_USER` (required): remote OS account used for all SSH requests; use a least-privileged account
- `SSH_TIMEOUT_SECONDS` (optional, default `20`, range 1–300): total SSH command execution deadline
- `SSH_MAX_OUTPUT_BYTES` (optional, default `16384`, range 1024–1048576): retained output bytes **per stream**
- `SSH_MAX_COMMAND_BYTES` (optional, default `4096`, range 1–1048576): maximum UTF-8 command input length

Do **not** set `TS_SSH_HOST`: each request supplies `host`. The service validates only host syntax (DNS/MagicDNS hostname or IP), **not** host authorization. Enable Tailscale SSH on each destination Linux machine with `sudo tailscale set --ssh`. Tailnet policy must allow TCP/22 and an SSH `accept` rule from the Cloud Run device/tag to the requested destination and login user. Tagged sources cannot use interactive SSH `check` mode.

Call from an authenticated client:

```bash
curl -sS -i -X POST "$URL/exec" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  --data '{"host":"edge-node-01","command":"uname -a"}'
```

Normal execution returns HTTP 200 with `host`, `exit_code`, `stdout`, `stderr`, `stdout_truncated`, `stderr_truncated`. An SSH connection error (unknown or offline host, network problem, SSH policy denial) **may also return HTTP 200** with a nonzero exit code and diagnostic `stderr`. Since the CLI uses OpenSSH, its exit code (commonly 255 for SSH connection errors) can overlap with legitimate remote command exit codes; this endpoint preserves diagnostics instead of guessing which failed. Inspect exit code and stderr. The bridge does not imply that HTTP 200 means remote execution succeeded.

Invalid inputs return HTTP 400/415, unconfigured SSH/Tailscale returns 503, CLI process spawn problems return 502, and timeout returns 504. A single timeout applies to setup and command execution. SSH subprocesses are launched without a **local** shell, but remote commands **are interpreted by the remote shell**, including metacharacters.

Output is drained continuously to avoid subprocess pipe deadlock and is held in memory only up to the configured bytes per stream. Extra output is discarded but consumed, allowing commands to finish normally. The request does not stream output and does not accept stdin. A timeout kills the local SSH process group; a background or detached **remote** process can survive. The current implementation does not guarantee cancelling remote work or enforce a concurrency cap. `tailscale status` Running only confirms the local daemon, not destination SSH reachability.
