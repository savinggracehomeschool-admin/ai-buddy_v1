"""Tests for the admin-alert dispatch path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _stub_settings(**overrides) -> SimpleNamespace:
    """Build a settings-shaped object for patching."""
    base = {"admin_canvas_user_id": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_alert_returns_false_when_no_admin_configured() -> None:
    from sgeg_nudge import alerts

    with patch.object(alerts, "settings", _stub_settings(admin_canvas_user_id=None)):
        assert alerts.send_alert_to_admin("test", "body") is False


def test_alert_calls_canvas_when_admin_configured() -> None:
    from sgeg_nudge import alerts

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None

    with patch.object(alerts, "settings", _stub_settings(admin_canvas_user_id=12345)), \
         patch("sgeg_nudge.canvas.CanvasClient", return_value=fake_client):
        ok = alerts.send_alert_to_admin("Test alert", "It broke")
        assert ok is True
        fake_client.send_conversation.assert_called_once()
        kwargs = fake_client.send_conversation.call_args.kwargs
        assert kwargs["recipient_ids"] == [12345]
        assert "It broke" in kwargs["body"]
        assert "Test alert" in kwargs["subject"]
        assert "SGEG Nudge Engine" in kwargs["subject"]


def test_alert_swallows_canvas_errors() -> None:
    """If Canvas call raises, alert returns False — must not propagate."""
    from sgeg_nudge import alerts

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.send_conversation.side_effect = RuntimeError("Canvas is down")

    with patch.object(alerts, "settings", _stub_settings(admin_canvas_user_id=12345)), \
         patch("sgeg_nudge.canvas.CanvasClient", return_value=fake_client):
        assert alerts.send_alert_to_admin("Test", "body") is False
