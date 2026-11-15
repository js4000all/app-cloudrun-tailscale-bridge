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
