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
