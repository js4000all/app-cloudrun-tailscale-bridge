# app-cloudrun-tailscale-bridge

A Python/Flask HTTP service for Google Cloud Run that joins a Tailscale tailnet in userspace mode and forwards authenticated HTTP requests to Tailscale SSH. The caller supplies the host, login user, and command.


## Getting started

High-level steps to connect a trusted HTTP caller (such as Google Apps Script) to a Tailscale SSH-enabled edge host. GAS custom-audience/token configuration is documented separately.

### 1. Tailscale: bridge and edge

1. Join the destination Linux host to the tailnet, assign it `tag:edge`, ensure the intended OS login account exists, and enable Tailscale SSH using `sudo tailscale set --ssh`.
2. Define owners for `tag:bridge` and `tag:edge` in your tailnet policy. Allow **both** TCP/22 network access from `tag:bridge` to `tag:edge` and an SSH `accept` rule for permitted destination OS accounts. This sample is a **fragment to merge into your existing policy** (not a complete replacement):
   ```json
   {
     "tagOwners": {
       "tag:bridge": ["autogroup:admin"],
       "tag:edge": ["autogroup:admin"]
     },
     "grants": [
       {"src": ["tag:bridge"], "dst": ["tag:edge"], "ip": ["tcp:22"]}
     ],
     "ssh": [
       {"action": "accept", "src": ["tag:bridge"], "dst": ["tag:edge"], "users": ["bridge"]}
     ]
   }
   ```
   Replace the example OS username `bridge` with the accounts you intend to allow. Tagged source devices cannot use interactive SSH `check` rules. Actual authorization is enforced by the Tailscale policy, not by an application-side allowlist.
3. Generate a tagged auth key with **Reusable = true**, **Ephemeral = true**, and **tag:bridge** assigned. The service uses `--state=mem:` and authenticates each new Cloud Run instance as a new node; a non-reusable key will fail on later starts. Auth keys expire (maximum 90 days), so plan rotation.

### 2. Google Cloud project and Secret Manager

1. Choose a project with billing enabled and enable the necessary APIs: Cloud Run, Cloud Build, Artifact Registry, and Secret Manager. Set up the appropriate build/deploy permissions for deploying from GitHub.
2. Create a Secret Manager secret containing the Tailscale auth key (for example, `tailscale-auth-key`). Never commit it or include it directly in build logs or the image.
3. When rotating the key, add a secret version and make sure a new Cloud Run revision receives the updated version.

### 3. IAM: service account and caller

1. Create a **dedicated Cloud Run runtime service account**, for example `cloudrun-tailscale-bridge`.
2. Grant this account `roles/secretmanager.secretAccessor` on the auth-key secret (not necessarily the whole project).
3. Grant `roles/run.invoker` on the Cloud Run service only to the trusted HTTP calling principal. The caller and the Cloud Run runtime service account are different identities.
4. Ensure your deployer has permission to deploy and attach the runtime service account (`roles/iam.serviceAccountUser`). The Cloud Build identity also needs its own build/deploy permissions; it is separate from the Cloud Run runtime identity.

### 4. Cloud Run: deploy from this repository

1. Create a Cloud Run **service** from this GitHub repository, using its `Dockerfile`. Configure the repository connection/Cloud Build trigger if you want automatic deployments.
2. Choose a region, assign the runtime service account, and select **Require authentication**. For GAS to call the service's public URL, configure ingress **All** while retaining IAM authentication. Do **not** permit unauthenticated calls.
3. Map the Secret Manager secret to environment variable **`TS_AUTHKEY`** (required). `TS_HOSTNAME` is optional. Do not set `TS_SSH_HOST` or `TS_SSH_USER`: the HTTP request supplies `host` and `user`.
4. Deploy. Tailscale must authenticate before Gunicorn starts listening on the Cloud Run `PORT` (default `8080`). If startup fails, inspect the revision logs, especially for invalid/expired auth keys.
5. Invoke `GET /health`, then `POST /exec` as an authenticated caller:
   ```json
   {"host":"edge-node-01","user":"bridge","command":"uname -a"}
   ```
   Check `exit_code` and `stderr`, not only HTTP 200. Unknown hosts and denied SSH logins are reported by the SSH CLI in the response.

**Security:** This is an arbitrary remote command API. Restrict HTTP callers via Cloud Run IAM and destination hosts/users via Tailscale policies. Review permitted OS account privileges, key rotation, concurrency, and operational logging before production use.


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

This service provides Tailnet connectivity and low-level SSH command forwarding. Structured edge information collection, interactive SSH, and command-level authorization are outside its current scope.

References: https://tailscale.com/docs/install/cloud/cloudrun and https://tailscale.com/docs/concepts/userspace-networking.


## SSH command execution proof of concept

**Security boundary:** `POST /exec` accepts arbitrary remote shell commands, a target host, and an OS login user. Protect the Cloud Run service with **Require authentication**, grant `roles/run.invoker` only to trusted callers, and do not deploy this API anonymously. Tailscale ACLs/grants and SSH policy determine which Tailnet hosts and remote OS accounts can be reached. No duplicate application-level host allowlist is maintained. Do not accept command strings from untrusted callers.

Configure the Cloud Run environment:

- `SSH_TIMEOUT_SECONDS` (optional, default `20`, range 1–300): total SSH command execution deadline
- `SSH_MAX_OUTPUT_BYTES` (optional, default `16384`, range 1024–1048576): retained output bytes **per stream**
- `SSH_MAX_COMMAND_BYTES` (optional, default `4096`, range 1–1048576): maximum UTF-8 command input length

Do **not** set `TS_SSH_HOST`: each request supplies `host`. The service validates host and OS login username syntax, **not** their authorization. The caller supplies `user` on each request; `TS_SSH_USER` is no longer required. Enable Tailscale SSH on each destination Linux machine with `sudo tailscale set --ssh`. Tailnet policy must allow TCP/22 and an SSH `accept` rule from the Cloud Run device/tag to the requested destination and login user. Tagged sources cannot use interactive SSH `check` mode.

Call from an authenticated client:

```bash
curl -sS -i -X POST "$URL/exec" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  --data '{"host":"edge-node-01","user":"bridge","command":"uname -a"}'
```

Normal execution returns HTTP 200 with `host`, `user`, `exit_code`, `stdout`, `stderr`, `stdout_truncated`, `stderr_truncated`. An SSH connection error (unknown or offline host, network problem, SSH policy denial) **may also return HTTP 200** with a nonzero exit code and diagnostic `stderr`. Since the CLI uses OpenSSH, its exit code (commonly 255 for SSH connection errors) can overlap with legitimate remote command exit codes; this endpoint preserves diagnostics instead of guessing which failed. Inspect exit code and stderr. The bridge does not imply that HTTP 200 means remote execution succeeded.

Invalid inputs return HTTP 400/415, unconfigured SSH/Tailscale returns 503, CLI process spawn problems return 502, and timeout returns 504. A single timeout applies to setup and command execution. SSH subprocesses are launched without a **local** shell, but remote commands **are interpreted by the remote shell**, including metacharacters.

Output is drained continuously to avoid subprocess pipe deadlock and is held in memory only up to the configured bytes per stream. Extra output is discarded but consumed, allowing commands to finish normally. The request does not stream output and does not accept stdin. A timeout kills the local SSH process group; a background or detached **remote** process can survive. The current implementation does not guarantee cancelling remote work or enforce a concurrency cap. `tailscale status` Running only confirms the local daemon, not destination SSH reachability.
