"""Execute the integrated replay command, using explicitly synthetic fixtures."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_war_room_replay import START, boundaries, config, source

from expansion_hunter import cli
from expansion_hunter.models import Asset
from expansion_hunter.war_room_replay import replay_war_room


def files(tmp_path):
    snapshot = source()
    snapshot_path = tmp_path / "synthetic-snapshot.json"
    config_path = tmp_path / "replay-settings.json"
    snapshot_path.write_text(snapshot.model_dump_json(), encoding="utf-8")
    config_path.write_text(config().model_dump_json(), encoding="utf-8")
    return snapshot, snapshot_path, config_path


def invoke(monkeypatch, snapshot, settings, points, *, output=None, extra=()):
    arguments = ["expansion-hunter", "war-room-replay", "--snapshot", str(snapshot),
                 "--replay-config", str(settings)]
    for when in points:
        arguments.extend(["--at", when if isinstance(when, str) else when.isoformat()])
    arguments.extend(extra)
    if output is not None:
        arguments.extend(["--output", str(output)])
    monkeypatch.setattr(sys, "argv", arguments)
    cli.main()


def test_executable_main_writes_same_causal_report_with_atomic_file_output(tmp_path, monkeypatch, capsys):
    snapshot, snapshot_path, config_path = files(tmp_path)
    destination = tmp_path / "reports" / "war-room-replay.json"
    points = [boundaries()[2], boundaries()[0], boundaries()[1], boundaries()[2]]
    monkeypatch.chdir(tmp_path)
    invoke(monkeypatch, snapshot_path, config_path, points, output=destination)
    written = json.loads(destination.read_text(encoding="utf-8"))
    assert written == replay_war_room(snapshot, points, settings=config())
    assert [row["fast_state"]["stage"] for row in written["evaluations"]] == ["BREAKOUT_PRESSURE", "1M_BREAKOUT_CONFIRMED", "5M_ACCEPTED"]
    assert capsys.readouterr().out == ""
    assert not list(tmp_path.rglob("*.sqlite3"))
    assert not list(tmp_path.rglob(".cockpit-*"))


def test_naive_timestamp_error_creates_no_report_or_partial_output(tmp_path, monkeypatch, caplog, capsys):
    _, snapshot_path, config_path = files(tmp_path)
    destination = tmp_path / "should-not-exist.json"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as raised:
        invoke(monkeypatch, snapshot_path, config_path, [START.replace(tzinfo=None).isoformat()], output=destination)
    assert raised.value.code == 1
    assert "timestamp must include timezone" in caplog.text
    assert not destination.exists()
    assert not list(tmp_path.rglob(".cockpit-*"))
    assert capsys.readouterr().out == ""


def test_wrong_btc_instrument_fails_without_publishing_report(tmp_path, monkeypatch, caplog):
    _, snapshot_path, config_path = files(tmp_path)
    wrong = source()
    wrong.asset = Asset(asset_id="ethereum", symbol="ETH", name="Ethereum", exchange_symbol="ETHUSDT")
    benchmark_path = tmp_path / "not-bitcoin.json"
    benchmark_path.write_text(wrong.model_dump_json(), encoding="utf-8")
    destination = tmp_path / "should-not-exist.json"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as raised:
        invoke(monkeypatch, snapshot_path, config_path, [START], output=destination,
               extra=["--btc", str(benchmark_path)])
    assert raised.value.code == 1
    assert "BTC benchmark instrument" in caplog.text
    assert not destination.exists()
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_bad_replay_does_not_overwrite_existing_evidence(tmp_path, monkeypatch):
    _, snapshot_path, config_path = files(tmp_path)
    destination = tmp_path / "existing-evidence.json"
    original = '{"keep":"existing evidence"}\n'
    destination.write_text(original, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, snapshot_path, config_path, ["not-a-timestamp"], output=destination)
    assert destination.read_text(encoding="utf-8") == original
    assert not list(tmp_path.rglob(".cockpit-*"))


def test_actual_btc_and_eth_snapshot_arguments_are_used_in_json_stdout(tmp_path, monkeypatch, capsys):
    snapshot, snapshot_path, config_path = files(tmp_path)
    extra = []
    benchmarks = {}
    for symbol, identity in (("BTC", "bitcoin"), ("ETH", "ethereum")):
        benchmark = source()
        benchmark.asset = Asset(asset_id=identity, symbol=symbol, name=identity,
                                exchange_symbol=symbol + "USDT")
        path = tmp_path / (identity + ".json")
        path.write_text(benchmark.model_dump_json(), encoding="utf-8")
        extra.extend(["--" + symbol.lower(), str(path)])
        benchmarks[symbol] = benchmark
    monkeypatch.chdir(tmp_path)
    invoke(monkeypatch, snapshot_path, config_path, boundaries()[:2], extra=extra)
    written = json.loads(capsys.readouterr().out)
    assert written == replay_war_room(snapshot, boundaries()[:2], btc=benchmarks["BTC"],
                                     eth=benchmarks["ETH"], settings=config())
    row = written["evaluations"][-1]["relative_context"]["windows"]["15m"]
    assert row["vs_btc"]["value"] == row["vs_eth"]["value"] == pytest.approx(0)
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_python_module_entrypoint_runs_offline_and_outputs_valid_report(tmp_path):
    _, snapshot_path, config_path = files(tmp_path)
    project_source = str(Path(__file__).resolve().parents[1] / "src")
    environment = {**os.environ, "PYTHONPATH": project_source}
    completed = subprocess.run([sys.executable, "-m", "expansion_hunter.cli", "war-room-replay",
                                "--snapshot", str(snapshot_path), "--replay-config", str(config_path),
                                "--at", START.isoformat()], cwd=tmp_path, env=environment,
                               capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["mode"] == "HISTORICAL_REPLAY"
    assert result["evaluations"][0]["fast_state"]["stage"] == "BREAKOUT_PRESSURE"
    assert result["execution_enabled"] is False
    assert not list(tmp_path.rglob("*.sqlite3"))
