"""Desktop controller and real protected-backend contracts, not Windows GUI proof."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from expansion_hunter.config import Settings
from expansion_hunter.desktop import (
    DesktopController,
    DesktopPaths,
    SingleInstance,
    backup_existing_database,
    prepare_desktop,
    startup_html,
)


def resources(tmp_path):
    resource = tmp_path / "resources"
    (resource / "config").mkdir(parents=True)
    (resource / "config" / "settings.json").write_text(Settings(
        top_n=500, symbol_map_path="config/symbols.json",
        binance_spot_url="https://data-api.binance.vision",
    ).model_dump_json())
    (resource / "config" / "symbols.json").write_text("{}")
    return DesktopPaths(tmp_path / "user-app-data", resource)


def test_desktop_settings_are_per_user_and_keep_public_provider_config(tmp_path):
    paths = resources(tmp_path)
    settings = prepare_desktop(paths)
    assert Path(settings.db_path) == paths.database
    assert settings.top_n == 500
    assert settings.binance_spot_url == "https://data-api.binance.vision"
    assert Path(settings.symbol_map_path) == paths.resources / "config" / "symbols.json"
    assert Settings.model_validate_json(paths.settings.read_text()).db_path == settings.db_path
    assert "COCKPIT_API_TOKEN" not in paths.settings.read_text()


def test_backup_includes_committed_wal_and_never_overwrites_user_database(tmp_path):
    source, target = tmp_path / "existing.sqlite3", tmp_path / "new.sqlite3"
    db = sqlite3.connect(source)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE evidence(value TEXT)")
    db.execute("INSERT INTO evidence VALUES('original preserved')")
    db.commit()
    try:
        assert backup_existing_database(source, target)
        with sqlite3.connect(target) as copied:
            assert copied.execute("SELECT value FROM evidence").fetchone()[0] == "original preserved"
        db.execute("INSERT INTO evidence VALUES('later')")
        db.commit()
        assert backup_existing_database(source, target) is False
        with sqlite3.connect(target) as copied:
            assert copied.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 2
    finally:
        db.close()


def test_invalid_database_import_does_not_publish_partial_destination(tmp_path):
    source, target = tmp_path / "invalid.sqlite3", tmp_path / "user.sqlite3"
    source.write_bytes(b"Not SQLite")
    with pytest.raises(sqlite3.DatabaseError):
        backup_existing_database(source, target)
    assert not target.exists()
    assert not target.with_name(target.name + ".importing").exists()
    assert source.read_bytes() == b"Not SQLite"


def test_second_desktop_launch_requests_focus_and_release_allows_restart(tmp_path):
    first, second = SingleInstance(tmp_path), SingleInstance(tmp_path)
    assert first.acquire()
    try:
        assert not second.acquire()
        assert first.focus_file.exists()
    finally:
        second.close()
        first.close()
    third = SingleInstance(tmp_path)
    try:
        assert third.acquire()
    finally:
        third.close()


def test_session_is_injected_only_into_owned_origin_and_never_into_url(tmp_path):
    paths = resources(tmp_path)
    controller = DesktopController(paths)
    controller.origin = "http://127.0.0.1:49123"

    class Window:
        current = "https://external.invalid/"
        scripts = []

        def get_current_url(self):
            return self.current

        def evaluate_js(self, script):
            self.scripts.append(script)

    window = Window()
    controller.inject_session(window)
    assert window.scripts == []
    window.current = controller.origin + "/cockpit"
    controller.inject_session(window)
    assert len(window.scripts) == 1
    assert "eh-desktop-session" in window.scripts[0]
    assert controller.token in window.scripts[0]
    assert controller.token not in controller.origin
    assert "localStorage" not in window.scripts[0]


def test_startup_screen_does_not_require_programming_or_claim_live_feed():
    screen = startup_html("<unsafe>& real connection pending")
    assert "&lt;unsafe&gt;" in screen
    assert "127.0.0.1" not in screen and "PowerShell" not in screen
    assert "No orders are placed" in screen
    assert 'lang="en"' in screen and "Starting Expansion Hunter" in screen
    failure = startup_html("The market service is unavailable.", failed=True)
    assert "Startup failed" in failure and "The market service is unavailable." in failure


def test_real_private_backend_random_port_auth_database_and_graceful_shutdown(tmp_path):
    paths = resources(tmp_path)
    prepare_desktop(paths)
    ready, stop = tmp_path / "ready.json", tmp_path / "stop.request"
    token = "deterministic-test-token-not-a-real-credential"  # Explicit local test fixture.
    environment = dict(os.environ, COCKPIT_API_TOKEN=token,
        PYTHONPATH=str(Path(__file__).parents[1] / "src"))
    process = subprocess.Popen([
        sys.executable, "-m", "expansion_hunter.desktop_backend", "--settings", str(paths.settings),
        "--ready-file", str(ready), "--stop-file", str(stop), "--no-auto-start",
    ], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1])
            time.sleep(0.05)
        assert ready.exists(), "Real backend did not start"
        marker = json.loads(ready.read_text())
        assert marker["backend"] == "STARTED"
        assert marker["market_feed"] == "NOT_YET_VERIFIED"
        assert marker["live_trading"] is False and marker["pid"] == process.pid
        assert token not in ready.read_text()
        with httpx.Client(trust_env=False, timeout=3) as client:
            origin = "http://127.0.0.1:" + str(marker["port"])
            assert client.get(origin + "/api/watchlist").status_code == 401
            response = client.get(origin + "/api/live/health", headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200
            assert response.json()["database"] == "AVAILABLE"
            assert response.json()["feed_fresh"] is False
            assert client.get(origin + "/cockpit").status_code == 200
        stop.write_text("STOP")
        process.wait(timeout=10)
        assert process.returncode == 0 and not ready.exists()
        with sqlite3.connect(paths.database) as db:
            assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        stdout, stderr = process.communicate()
        assert token not in stdout + stderr
