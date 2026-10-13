"""Optional memory budgets preserve live signal wiring without external feeds."""

import asyncio
import sys

import pytest

from expansion_hunter import cli
from expansion_hunter.api import create_app
from expansion_hunter.config import Settings
from expansion_hunter.trade_alert_models import TradeAlertSettings


@pytest.mark.parametrize("value, enabled", [
    (None, True), ("true", True), ("false", False), ("0", False), (" NO ", False),
])
async def test_research_switch_preserves_live_workers(tmp_path, monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("COCKPIT_RESEARCH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("COCKPIT_RESEARCH_ENABLED", value)
    started, stopped = set(), set()

    def worker(name):
        async def run(app, *args):
            started.add(name)
            try:
                await asyncio.Future()
            finally:
                stopped.add(name)
        return run

    workers = {
        "native": "expansion_hunter.trade_alert_runtime.run_trade_alert_worker",
        "deep": "expansion_hunter.pre_move_runtime.run_pre_move_deep_worker",
        "notifications": "expansion_hunter.notification_runtime.run_notification_worker",
        "trade_research": "expansion_hunter.trade_alert_research.run_trade_alert_research_worker",
        "daily_research": "expansion_hunter.pre_ignition_research.run_pre_ignition_research_worker",
    }
    for name, path in workers.items():
        monkeypatch.setattr(path, worker(name))
    app = create_app(Settings(db_path=str(tmp_path / "signals.sqlite3")),
                     trade_alert_settings=TradeAlertSettings(max_assets=2))
    expected = {"native", "deep", "notifications"}
    if enabled:
        expected.update({"trade_research", "daily_research"})
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert started == expected
        if not enabled:
            for state in (app.state.trade_alert_research_health, app.state.pre_ignition_research_health):
                assert state["status"] == "DISABLED"
                assert state["reason"] == "Offline research disabled by COCKPIT_RESEARCH_ENABLED"
    assert stopped == expected


@pytest.fixture
def capture_signal_config(tmp_path, monkeypatch):
    import uvicorn

    alert_path = tmp_path / "alerts.json"
    alert_path.write_text(TradeAlertSettings().model_dump_json())
    monkeypatch.delenv("COCKPIT_FAST_MONITOR_LIMIT", raising=False)
    monkeypatch.delenv("COCKPIT_DEEP_WATCH_LIMIT", raising=False)
    monkeypatch.delenv("COCKPIT_PRE_IGNITION_ENABLED", raising=False)
    monkeypatch.setattr(cli, "load_command_settings", lambda args: Settings())
    captured = {}

    def capture_app(settings, *, trade_alert_settings, pre_ignition_settings):
        captured["config"] = trade_alert_settings or pre_ignition_settings
        return object()

    monkeypatch.setattr("expansion_hunter.api.create_app", capture_app)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: captured.update(served=True))

    def run(command):
        monkeypatch.setattr(sys, "argv", ["expansion-hunter", command, "--alert-config", str(alert_path)])
        cli.main()
        return captured

    return run


@pytest.mark.parametrize("command", ["app", "trade-alert"])
@pytest.mark.parametrize("deep_limit, monitor_limit, expected_assets, expected_deep", [
    (None, None, 500, 25),
    ("5", None, 500, 5),
    ("0", None, 500, 0),
    ("50", None, 500, 50),
    ("50", "7", 7, 7),
])
def test_deep_budget_is_independent_of_native_coverage(
    monkeypatch, capture_signal_config, command, deep_limit, monitor_limit, expected_assets, expected_deep,
):
    if deep_limit is not None:
        monkeypatch.setenv("COCKPIT_DEEP_WATCH_LIMIT", deep_limit)
    if monitor_limit is not None:
        monkeypatch.setenv("COCKPIT_FAST_MONITOR_LIMIT", monitor_limit)
    captured = capture_signal_config(command)
    assert captured["served"] is True
    assert captured["config"].max_assets == expected_assets
    assert captured["config"].deep_watch_limit == expected_deep


@pytest.mark.parametrize("value", ["-1", "51", "invalid"])
def test_invalid_deep_budget_is_rejected(monkeypatch, capture_signal_config, caplog, value):
    monkeypatch.setenv("COCKPIT_DEEP_WATCH_LIMIT", value)
    with pytest.raises(SystemExit) as raised:
        capture_signal_config("trade-alert")
    assert raised.value.code == 1
    if value != "invalid":
        assert "COCKPIT_DEEP_WATCH_LIMIT must be between 0 and 50" in caplog.text
