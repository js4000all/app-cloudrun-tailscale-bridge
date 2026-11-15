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
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    result = {
        "host": "edge1", "exit_code": 0, "stdout": "hello\n", "stderr": "",
        "stdout_truncated": False, "stderr_truncated": False,
    }
    with patch("src.main.get_tailscale_backend_state", return_value="Running"), \
         patch("src.main.run_ssh_command", return_value=result) as runner:
        response = app.test_client().post("/exec", json={"host": "edge1", "command": "id"})
    assert response.status_code == 200
    assert response.get_json() == result
    runner.assert_called_once_with("edge1", "id")


def test_exec_invalid_host(monkeypatch):
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    for host in ["", "-oProxyCommand=bad", "host;id", "evil name", "a..b", None, 4, "a" * 64]:
        response = app.test_client().post("/exec", json={"host": host, "command": "id"})
        assert response.status_code == 400
    from src.main import valid_host
    assert valid_host("100.101.102.103")
    assert valid_host("edge-node.example.ts.net")


def test_exec_invalid_command(monkeypatch):
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    for command in ["", " ", "a" * 4097, 42, "a\x00b"]:
        response = app.test_client().post("/exec", json={"host": "edge", "command": command})
        assert response.status_code == 400


def test_exec_missing_user(monkeypatch):
    monkeypatch.delenv("TS_SSH_USER", raising=False)
    response = app.test_client().post("/exec", json={"host": "edge", "command": "id"})
    assert response.status_code == 503


def test_exec_disconnected(monkeypatch):
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    with patch("src.main.get_tailscale_backend_state", return_value="NeedsLogin"):
        response = app.test_client().post("/exec", json={"host": "edge", "command": "id"})
    assert response.status_code == 503


def test_exec_timeout(monkeypatch):
    import subprocess
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    with patch("src.main.get_tailscale_backend_state", return_value="Running"), \
         patch("src.main.run_ssh_command", side_effect=subprocess.TimeoutExpired("ssh", 20)):
        response = app.test_client().post("/exec", json={"host": "edge", "command": "sleep 60"})
    assert response.status_code == 504


def test_exec_nonzero_exit_and_stderr(monkeypatch):
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    with patch("src.main.get_tailscale_backend_state", return_value="Running"), \
         patch("src.main.run_ssh_command", return_value={
             "host": "absent", "exit_code": 255, "stdout": "",
             "stderr": "ssh failed", "stdout_truncated": False,
             "stderr_truncated": False,
         }):
        response = app.test_client().post("/exec", json={"host": "absent", "command": "id"})
    assert response.status_code == 200
    assert response.get_json()["exit_code"] == 255
    assert response.get_json()["stderr"] == "ssh failed"


def test_ssh_subprocess_streams(monkeypatch):
    from src.main import run_ssh_command
    monkeypatch.setenv("TS_SSH_USER", "bridge")
    monkeypatch.setenv("TS_SOCKET_PATH", "/tmp/custom.sock")
    monkeypatch.setenv("SSH_MAX_OUTPUT_BYTES", "1024")
    class FakePipe:
        def close(self):
            pass
    class FakeProcess:
        stdout = FakePipe()
        stderr = FakePipe()
        def wait(self, timeout=None):
            return 7
    with patch("src.main.subprocess.Popen", return_value=FakeProcess()) as popen, \
         patch("src.main.selectors.DefaultSelector") as selector_cls, \
         patch("src.main.os.read", side_effect=[b"o" * 2048, b"", b"warning", b""]):
        sel = selector_cls.return_value.__enter__.return_value
        one = type("Key", (), {"fileobj": FakePipe(), "data": "stdout"})
        two = type("Key", (), {"fileobj": FakePipe(), "data": "stderr"})
        sel.get_map.side_effect = [True, True, True, True, False]
        sel.select.side_effect = [[(one, None)], [(one, None)], [(two, None)], [(two, None)]]
        result = run_ssh_command("edge", "id")
    assert popen.call_args.args[0] == [
        "tailscale", "--socket=/tmp/custom.sock", "ssh", "bridge@edge", "id"
    ]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert result["exit_code"] == 7
    assert result["stdout"] == "o" * 1024
    assert result["stdout_truncated"] is True
    assert result["stderr"] == "warning"
