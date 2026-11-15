import json
import os
import subprocess

from flask import Flask, jsonify

app = Flask(__name__)


def get_tailscale_backend_state():
    socket_path = os.environ.get("TS_SOCKET_PATH", "/tmp/tailscaled.sock")
    try:
        result = subprocess.run(
            ["tailscale", f"--socket={socket_path}", "status", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        status = json.loads(result.stdout)
        backend_state = status.get("BackendState")
        return backend_state if isinstance(backend_state, str) else "Unknown"
    except (OSError, subprocess.SubprocessError, ValueError):
        return "Unavailable"


@app.get("/health")
def health():
    state = get_tailscale_backend_state()
    connected = state == "Running"
    return (
        jsonify(
            status="ok" if connected else "error",
            tailscale={"connected": connected, "backend_state": state},
        ),
        200 if connected else 503,
    )


def run_ssh_command(command):
    """Execute a remote command without invoking a local shell."""
    import tempfile

    socket_path = os.environ.get("TS_SOCKET_PATH", "/tmp/tailscaled.sock")
    host = os.environ["TS_SSH_HOST"]
    user = os.environ["TS_SSH_USER"]
    argv = ["tailscale", f"--socket={socket_path}", "ssh", f"{user}@{host}", command]
    with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            timeout=20,
            check=False,
        )
        limit = 16 * 1024
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read(limit + 1)
        errors = stderr.read(limit + 1)
        return {
            "exit_code": result.returncode,
            "stdout": output[:limit].decode("utf-8", errors="replace"),
            "stderr": errors[:limit].decode("utf-8", errors="replace"),
            "stdout_truncated": len(output) > limit,
            "stderr_truncated": len(errors) > limit,
        }


@app.post("/exec")
def execute():
    from flask import request

    if not request.is_json:
        return jsonify(error="JSON request required"), 415

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("command"), str):
        return jsonify(error="command must be a string"), 400

    command = payload["command"]
    if not command.strip() or len(command.encode("utf-8")) > 4096 or "\x00" in command:
        return jsonify(error="command must be non-empty and at most 4096 bytes"), 400

    if not os.environ.get("TS_SSH_HOST") or not os.environ.get("TS_SSH_USER"):
        return jsonify(error="SSH target is not configured"), 503
    if get_tailscale_backend_state() != "Running":
        return jsonify(error="Tailscale is not connected"), 503

    try:
        return jsonify(run_ssh_command(command)), 200
    except subprocess.TimeoutExpired:
        return jsonify(error="SSH command timed out"), 504
    except (OSError, subprocess.SubprocessError):
        app.logger.exception("SSH command invocation failed")
        return jsonify(error="SSH command invocation failed"), 502
