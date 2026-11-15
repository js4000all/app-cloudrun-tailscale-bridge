import json
import ipaddress
import os
import re
import selectors
import signal
import subprocess
import time

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



HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def valid_host(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return all(HOST_LABEL.fullmatch(label) for label in host.split("."))


def setting_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def run_ssh_command(host, command):
    """Drain SSH output as it arrives, retaining only bounded response buffers."""
    user = os.environ["TS_SSH_USER"]
    socket_path = os.environ.get("TS_SOCKET_PATH", "/tmp/tailscaled.sock")
    timeout = setting_int("SSH_TIMEOUT_SECONDS", 20, 1, 300)
    limit = setting_int("SSH_MAX_OUTPUT_BYTES", 16384, 1024, 1048576)
    argv = ["tailscale", f"--socket={socket_path}", "ssh", f"{user}@{host}", command]

    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(remaining):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    name = key.data
                    space = max(0, limit - len(outputs[name]))
                    outputs[name].extend(data[:space])
                    if len(data) > space:
                        truncated[name] = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        exit_code = process.wait(timeout=remaining)
    except BaseException:
        # Kill the local tailscale/ssh process group. A detached remote task may survive.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    return {
        "host": host,
        "exit_code": exit_code,
        "stdout": outputs["stdout"].decode("utf-8", errors="replace"),
        "stderr": outputs["stderr"].decode("utf-8", errors="replace"),
        "stdout_truncated": truncated["stdout"],
        "stderr_truncated": truncated["stderr"],
    }


@app.post("/exec")
def execute():
    from flask import request

    if not request.is_json:
        return jsonify(error="JSON request required"), 415
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="JSON object required"), 400

    host = payload.get("host")
    command = payload.get("command")
    if not valid_host(host):
        return jsonify(error="host must be a valid MagicDNS hostname or IP address"), 400
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        return jsonify(error="command must be a non-empty string without NUL"), 400
    try:
        max_command = setting_int("SSH_MAX_COMMAND_BYTES", 4096, 1, 1048576)
        setting_int("SSH_TIMEOUT_SECONDS", 20, 1, 300)
        setting_int("SSH_MAX_OUTPUT_BYTES", 16384, 1024, 1048576)
    except ValueError:
        app.logger.exception("Invalid SSH limit configuration")
        return jsonify(error="SSH configuration invalid"), 503
    if len(command.encode("utf-8")) > max_command:
        return jsonify(error=f"command must be at most {max_command} bytes"), 400

    if not os.environ.get("TS_SSH_USER"):
        return jsonify(error="SSH user is not configured"), 503
    if get_tailscale_backend_state() != "Running":
        return jsonify(error="Tailscale is not connected"), 503
    try:
        # CLI and OpenSSH failures can share exit codes with remote commands.
        # Preserve the raw exit code and stderr; do not guess the failure category.
        return jsonify(run_ssh_command(host, command)), 200
    except subprocess.TimeoutExpired:
        return jsonify(error="SSH command timed out", host=host), 504
    except (OSError, subprocess.SubprocessError):
        app.logger.exception("SSH command invocation failed")
        return jsonify(error="SSH command invocation failed", host=host), 502
