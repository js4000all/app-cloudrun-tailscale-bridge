from unittest.mock import patch

from src.main import app, get_tailscale_backend_state


def test_health_running():
    with patch("src.main.get_tailscale_backend_state", return_value="Running"):
        response = app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "tailscale": {"connected": True, "backend_state": "Running"},
    }


def test_health_not_connected():
    with patch("src.main.get_tailscale_backend_state", return_value="NeedsLogin"):
        response = app.test_client().get("/health")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "error",
        "tailscale": {"connected": False, "backend_state": "NeedsLogin"},
    }


def test_health_tailscaled_unavailable():
    with patch("src.main.get_tailscale_backend_state", return_value="Unavailable"):
        response = app.test_client().get("/health")

    assert response.status_code == 503
    assert response.get_json()["tailscale"]["connected"] is False


def test_status_cli_running():
    with patch("src.main.subprocess.run") as run:
        run.return_value.stdout = '{"BackendState":"Running"}'
        assert get_tailscale_backend_state() == "Running"
        run.assert_called_once()
        assert run.call_args.kwargs["timeout"] == 3


def test_status_cli_unavailable():
    with patch("src.main.subprocess.run", side_effect=FileNotFoundError):
        assert get_tailscale_backend_state() == "Unavailable"


def test_status_cli_malformed():
    with patch("src.main.subprocess.run") as run:
        run.return_value.stdout = "not-json"
        assert get_tailscale_backend_state() == "Unavailable"


def test_exec_success(monkeypatch):
    from src.main import run_ssh_command
    import subprocess

    monkeypatch.setenv("TS_SSH_HOST", "edge-host")
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    with patch("src.main.get_tailscale_backend_state", return_value="Running"), \
         patch("src.main.run_ssh_command", return_value={
             "exit_code": 0, "stdout": "hello\n", "stderr": "",
             "stdout_truncated": False, "stderr_truncated": False
         }) as runner:
        response = app.test_client().post("/exec", json={"command": "printf hello"})
    assert response.status_code == 200
    assert response.get_json()["stdout"] == "hello\n"
    runner.assert_called_once_with("printf hello")


def test_exec_missing_target(monkeypatch):
    monkeypatch.delenv("TS_SSH_HOST", raising=False)
    monkeypatch.delenv("TS_SSH_USER", raising=False)
    response = app.test_client().post("/exec", json={"command": "id"})
    assert response.status_code == 503


def test_exec_invalid_command(monkeypatch):
    monkeypatch.setenv("TS_SSH_HOST", "edge-host")
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    for command in ["", " " * 3, "a" * 4097, 42]:
        response = app.test_client().post("/exec", json={"command": command})
        assert response.status_code == 400


def test_exec_disconnected(monkeypatch):
    monkeypatch.setenv("TS_SSH_HOST", "edge-host")
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    with patch("src.main.get_tailscale_backend_state", return_value="NeedsLogin"):
        response = app.test_client().post("/exec", json={"command": "id"})
    assert response.status_code == 503


def test_exec_timeout(monkeypatch):
    import subprocess
    monkeypatch.setenv("TS_SSH_HOST", "edge-host")
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    with patch("src.main.get_tailscale_backend_state", return_value="Running"), \
         patch("src.main.run_ssh_command", side_effect=subprocess.TimeoutExpired("ssh", 20)):
        response = app.test_client().post("/exec", json={"command": "sleep 60"})
    assert response.status_code == 504


def test_ssh_cli_arguments(monkeypatch):
    from src.main import run_ssh_command
    import subprocess

    monkeypatch.setenv("TS_SSH_HOST", "edge-host")
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    monkeypatch.setenv("TS_SOCKET_PATH", "/tmp/custom.sock")

    def fake_run(argv, **kwargs):
        kwargs["stdout"].write(b"ok\n")
        kwargs["stderr"].write(b"warning\n")
        return subprocess.CompletedProcess(argv, 7)

    with patch("src.main.subprocess.run", side_effect=fake_run) as runner:
        result = run_ssh_command("uname -a")
    assert runner.call_args.args[0] == [
        "tailscale", "--socket=/tmp/custom.sock", "ssh", "bridge@edge-host", "uname -a"
    ]
    assert result["exit_code"] == 7
    assert result["stdout"] == "ok\n"
    assert result["stderr"] == "warning\n"
