"""Operator CLI. Live scans, archived evaluation, replay, and read-only API."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .config import Settings
from .discovery_config import DiscoverySettings
from .models import AssetSnapshot, RegimeReport, aware

DISCOVERY_COMMANDS = {
    "discover", "discover-watch", "discovery-status", "discovery-events", "import-smoke",
    "app", "trade-alert", "trade-alert-test", "trade-alert-status", "trade-alert-replay", "live-monitor", "live-smoke", "watchlist", "intelligence", "macro-import", "macro-calendar",
}


def load_settings(path: str | None) -> Settings:
    return Settings.model_validate_json(Path(path).read_text()) if path else Settings()


def load_command_settings(args) -> Settings:
    """New discovery commands share the checked-in operational configuration.

    Existing commands retain their original defaults. An explicit --config
    always wins; smoke evidence is never selected implicitly.
    """
    path = args.config
    if path is None and args.command in DISCOVERY_COMMANDS and Path("config/settings.json").is_file():
        path = "config/settings.json"
    return load_settings(path)


def load_discovery_settings(path: str | None) -> DiscoverySettings:
    return DiscoverySettings.model_validate_json(Path(path).read_text()) if path else DiscoverySettings()


def write_output(data, path: str | None):
    text = (
        data.model_dump_json(indent=2)
        if hasattr(data, "model_dump_json")
        else json.dumps(data, indent=2, allow_nan=False)
    )
    if not path:
        print(text)
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=destination.parent, delete=False, prefix=".cockpit-", encoding="utf-8"
    ) as handle:
        handle.write(text + "\n")
        temp_path = handle.name
    os.replace(temp_path, destination)


def portfolio_from_file(path: str | None):
    if path is None:
        return None
    from .risk import PortfolioState

    return PortfolioState.model_validate_json(Path(path).read_text())


async def live(args, settings):
    from .runtime import runtime

    print(f"Operational database: {Path(settings.db_path).resolve()}", file=sys.stderr)
    async with runtime(settings) as scanner:
        while True:
            batch = await scanner.scan(portfolio=portfolio_from_file(args.portfolio))
            write_output(batch, args.output)
            logging.info(
                "Completed %s: evaluated=%d errors=%d", batch.scan_id, batch.evaluated, len(batch.errors)
            )
            if args.command == "scan":
                break
            await asyncio.sleep(args.interval)


async def discovery_live(args, settings: Settings, discovery_settings: DiscoverySettings):
    """Poll adaptive work without allowing a transient failure to kill watch.

    The engine decides which universe/deep work is due and persists provider
    failures. Unexpected errors remain visible and use capped backoff. A
    one-shot discovery command fails normally so automation sees its exit code.
    """
    from .runtime import discovery_runtime

    interval = args.poll_interval or discovery_settings.poll_interval_seconds
    if interval < 1:
        raise ValueError("discovery poll interval must be at least one second")
    print(f"Operational database: {Path(settings.db_path).resolve()}", file=sys.stderr)
    failures = 0
    cycles = 0
    async with discovery_runtime(settings, discovery_settings) as engine:
        while True:
            delay = interval
            try:
                cycle = await engine.run_once()
                write_output(cycle, args.output)
                failures = 0
                cycles += 1
                logging.info(
                    "Discovery %s: status=%s counts=%s database=%s",
                    cycle.cycle_id, cycle.status, cycle.counts, cycle.database_path,
                )
                if args.command == "discover" or (args.cycles and cycles >= args.cycles):
                    return
            except Exception as exc:
                if args.command == "discover":
                    raise
                failures += 1
                delay = min(300, interval * (2 ** min(failures, 8)))
                logging.error(
                    "Discovery cycle failed (%s: %s); retrying in %.1fs; database=%s",
                    type(exc).__name__, exc, delay, Path(settings.db_path).resolve(),
                )
                write_output({
                    "status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                    "database_path": str(Path(settings.db_path).resolve()),
                    "retry_in_seconds": delay,
                }, args.output)
            await asyncio.sleep(delay)


def parser():
    root = argparse.ArgumentParser(description="TRADING COCKPIT — EXPANSION HUNTER")
    root.add_argument("--config", help="Settings JSON (defaults are in config.py)")
    root.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("scan", "watch"):
        command = commands.add_parser(name, help="Collect live data and publish an auditable decision scan")
        command.add_argument("--output", help="Atomic JSON report path; otherwise stdout")
        command.add_argument(
            "--portfolio", help="Fresh verified portfolio risk state JSON; absent blocks ENTRY"
        )
        if name == "watch":
            command.add_argument(
                "--interval", type=float, default=300, help="Seconds after each scan (minimum 60)"
            )
    for name in ("discover", "discover-watch"):
        command = commands.add_parser(
            name, help="Run lightweight discovery and budgeted automatic deep analysis; no orders"
        )
        command.add_argument("--discovery-config", default="config/discovery.json")
        command.add_argument("--output", help="Atomic JSON cycle report; otherwise stdout")
        command.add_argument("--poll-interval", type=float, help="Adaptive work poll interval in seconds")
        command.add_argument(
            "--cycles", type=int, help="Stop watch after this many published cycles; default keeps running"
        )
    status = commands.add_parser("discovery-status", help="Read discovery state and operational database path")
    status.add_argument("--output")
    events = commands.add_parser("discovery-events", help="Read persisted discovery promotions/demotions")
    events.add_argument("--asset-id")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--output")
    migrate = commands.add_parser("import-smoke", help="Safely copy old scan evidence into the operational database")
    migrate.add_argument("--source", default="data/smoke.sqlite3")
    migrate.add_argument("--output")
    inspect = commands.add_parser("inspect", help="Read completed scans or archived asset snapshots")
    inspect.add_argument("--asset-id")
    inspect.add_argument("--scan-id")
    inspect.add_argument("--output")
    offline = commands.add_parser(
        "evaluate", help="Evaluate an archived snapshot with recorded regime context"
    )
    offline.add_argument("--snapshot", required=True)
    offline.add_argument("--regime", help="Recorded RegimeReport JSON; absence blocks ENTRY")
    offline.add_argument("--benchmark")
    offline.add_argument("--portfolio")
    offline.add_argument("--output")
    replay = commands.add_parser("replay", help="Replay only data available at specified timestamps")
    replay.add_argument("--snapshot", required=True)
    replay.add_argument("--benchmark")
    replay.add_argument(
        "--at", required=True, action="append", help="Aware ISO8601 UTC timestamp; repeatable"
    )
    replay.add_argument("--output")
    integrated_replay = commands.add_parser(
        "war-room-replay", help="Replay integrated structure, scenarios and fast alerts from point-in-time observations"
    )
    integrated_replay.add_argument("--snapshot", required=True)
    integrated_replay.add_argument("--btc", help="Dated BTC AssetSnapshot JSON, never an undated price")
    integrated_replay.add_argument("--eth", help="Dated ETH AssetSnapshot JSON")
    integrated_replay.add_argument("--at", required=True, action="append", help="Aware ISO8601 decision timestamp; repeatable")
    integrated_replay.add_argument("--replay-config", help="Explicit WarRoomReplaySettings JSON")
    integrated_replay.add_argument("--output")
    study = commands.add_parser(
        "research", help="Compare winners with contemporaneous pre-event lookalikes and failed setups"
    )
    study.add_argument(
        "--dataset", required=True, help="Point-in-time StudyDataset JSON; future outcomes must be separate"
    )
    study.add_argument(
        "--research-config", help="Locked ResearchSettings JSON including controls/horizon/train boundary"
    )
    study.add_argument("--output")
    schema = commands.add_parser(
        "research-schema", help="Export the historical matched-study input JSON schema"
    )
    schema.add_argument("--output")
    serve = commands.add_parser("serve", help="Serve read-only scan reports over FastAPI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    app = commands.add_parser("app", help="Start the observation cockpit and local live controls")
    app.add_argument("--host", default="127.0.0.1")
    app.add_argument("--port", type=int, default=8000)
    app.add_argument("--alert-config", default="config/trade_alert.json",
                     help="Native all-universe pre-ignition configuration alongside the existing cockpit")
    app.add_argument("--disable-pre-ignition", action="store_true",
                     help="Keep the legacy cockpit only; does not alter existing saved observations")
    alerts = commands.add_parser("trade-alert", help="Run the real spot LONG/SHORT signal scanner and dashboard")
    alerts.add_argument("--alert-config", default="config/trade_alert.json")
    alerts.add_argument("--host", default="127.0.0.1")
    alerts.add_argument("--port", type=int, default=8000)
    commands.add_parser("trade-alert-test", help="Explicit Telegram connectivity test; never a fictitious market alert")
    commands.add_parser("trade-alert-status", help="Report the latest persisted Trade Alert coverage with its actual age")
    replay_alerts = commands.add_parser("trade-alert-replay", help="Chronological Trade Alert replay and purged walk-forward reports")
    replay_alerts.add_argument("input")
    replay_alerts.add_argument("--alert-config", default="config/trade_alert.json")
    replay_alerts.add_argument("--output")
    stream = commands.add_parser("live-monitor", help="Independent public WebSocket fast watcher")
    stream.add_argument("--symbol", action="append", default=[])
    stream.add_argument("--seconds", type=float)
    stream.add_argument("--output")
    smoke = commands.add_parser("live-smoke", help="Bounded real public WebSocket check, no synthetic fallback")
    smoke.add_argument("--symbol", action="append", default=[])
    smoke.add_argument("--seconds", type=float, default=20)
    smoke.add_argument("--output")
    watchlist = commands.add_parser("watchlist", help="Persistent personal watchlist, no score boost")
    watchlist.add_argument("action", choices=["list", "add", "remove"])
    watchlist.add_argument("--symbol")
    watchlist.add_argument("--asset-id")
    watchlist.add_argument("--priority", type=int, default=0)
    watchlist.add_argument("--pinned", action="store_true")
    watchlist.add_argument("--notes", default="")
    watchlist.add_argument("--output")
    intelligence = commands.add_parser("intelligence", help="Background cycle/macro/analogue/outcome worker")
    intelligence.add_argument("--watch", action="store_true")
    intelligence.add_argument("--interval", type=float, default=60)
    intelligence.add_argument("--output")
    macro = commands.add_parser("macro-import", help="Import dated macro schedules/consensus/initial/revisions")
    macro.add_argument("--file", required=True)
    macro.add_argument("--output")
    calendar = commands.add_parser("macro-calendar", help="Fetch authoritative BLS schedules; numeric releases unavailable")
    calendar.add_argument("--days", type=int, default=7)
    calendar.add_argument("--output")
    return root


def main():
    args = parser().parse_args()
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    try:
        settings = load_command_settings(args)
        if args.command in {"scan", "watch"}:
            if args.command == "watch" and args.interval < 60:
                raise ValueError("watch interval must be at least 60 seconds")
            asyncio.run(live(args, settings))
        elif args.command in {"discover", "discover-watch"}:
            if args.cycles is not None and args.cycles < 1:
                raise ValueError("cycles must be positive")
            asyncio.run(discovery_live(args, settings, load_discovery_settings(args.discovery_config)))
        elif args.command in {"discovery-status", "discovery-events", "import-smoke"}:
            from .discovery_store import DiscoveryStore

            store = DiscoveryStore(settings.db_path)
            print(f"Operational database: {store.database_path}", file=sys.stderr)
            try:
                if args.command == "discovery-events":
                    if not 1 <= args.limit <= 1000:
                        raise ValueError("event limit must be between 1 and 1000")
                    write_output({
                        "database_path": store.database_path,
                        "events": [event.model_dump(mode="json") for event in store.events(
                            asset_id=args.asset_id, limit=args.limit
                        )],
                    }, args.output)
                elif args.command == "import-smoke":
                    write_output({
                        "database_path": store.database_path,
                        "source": str(Path(args.source).resolve()),
                        "import": store.import_legacy(args.source),
                    }, args.output)
                else:
                    cycle = store.latest_cycle()
                    write_output({
                        "database_path": store.database_path,
                        "live_trading_enabled": False,
                        "latest_cycle": cycle.model_dump(mode="json") if cycle else None,
                    }, args.output)
            finally:
                store.close()
        elif args.command == "inspect":
            from .storage import Store

            store = Store(settings.db_path)
            try:
                if args.asset_id:
                    snapshots = store.snapshots(args.asset_id)
                    if not snapshots:
                        raise ValueError("no completed snapshots for that asset")
                    write_output(snapshots[-1], args.output)
                else:
                    batch = store.get_batch(args.scan_id) if args.scan_id else store.latest()
                    if batch is None:
                        raise ValueError("no completed scan available")
                    write_output(batch, args.output)
            finally:
                store.close()
        elif args.command == "war-room-replay":
            from .war_room_replay import WarRoomReplaySettings, replay_war_room

            snapshot = AssetSnapshot.model_validate_json(Path(args.snapshot).read_text())
            btc = AssetSnapshot.model_validate_json(Path(args.btc).read_text()) if args.btc else None
            eth = AssetSnapshot.model_validate_json(Path(args.eth).read_text()) if args.eth else None
            replay_settings = (WarRoomReplaySettings.model_validate_json(Path(args.replay_config).read_text())
                               if args.replay_config else None)
            write_output(replay_war_room(snapshot, [aware(datetime.fromisoformat(at)) for at in args.at],
                                        btc=btc, eth=eth, settings=replay_settings), args.output)
        elif args.command in {"evaluate", "replay"}:
            snapshot = AssetSnapshot.model_validate_json(Path(args.snapshot).read_text())
            benchmark = (
                AssetSnapshot.model_validate_json(Path(args.benchmark).read_text())
                if args.benchmark
                else None
            )
            if args.command == "replay":
                from .replay import replay

                reports = replay(
                    snapshot,
                    [aware(datetime.fromisoformat(x.replace("Z", "+00:00"))) for x in args.at],
                    settings,
                    benchmark,
                )
                write_output([r.model_dump(mode="json") for r in reports], args.output)
            else:
                from .replay import point_in_time
                from .rules import evaluate
                from .technical import analyze

                snapshot = point_in_time(snapshot, snapshot.as_of)
                regime = (
                    RegimeReport.model_validate_json(Path(args.regime).read_text())
                    if args.regime
                    else RegimeReport(
                        missing=["recorded_regime"], reasons=["No recorded regime supplied; entries disabled"]
                    )
                )
                tech = {tf: analyze(bars, tf, snapshot.as_of) for tf, bars in snapshot.candles.items()}
                write_output(
                    evaluate(
                        snapshot,
                        tech,
                        regime,
                        settings,
                        benchmark=benchmark,
                        portfolio=portfolio_from_file(args.portfolio),
                    ),
                    args.output,
                )
        elif args.command in {"serve", "app", "trade-alert"}:
            import uvicorn

            from .api import create_app

            print(f"Operational database: {Path(settings.db_path).resolve()}", file=sys.stderr)
            alert_config = None
            pre_ignition_config = None
            if args.command == "trade-alert":
                from .trade_alert_models import TradeAlertSettings
                alert_config = TradeAlertSettings.model_validate_json(Path(args.alert_config).read_text())
            elif args.command == "app" and not args.disable_pre_ignition and os.getenv("COCKPIT_PRE_IGNITION_ENABLED", "true").lower() not in {"false", "0", "no"}:
                from .trade_alert_models import TradeAlertSettings

                path = Path(args.alert_config)
                pre_ignition_config = TradeAlertSettings.model_validate_json(path.read_text()) if path.is_file() else TradeAlertSettings()
            monitor_limit = os.getenv("COCKPIT_FAST_MONITOR_LIMIT")
            if monitor_limit:
                limit = int(monitor_limit)
                if not 1 <= limit <= 500:
                    raise ValueError("COCKPIT_FAST_MONITOR_LIMIT must be between 1 and 500")
                for config in (alert_config, pre_ignition_config):
                    if config is not None:
                        config.max_assets = min(config.max_assets, limit)
                        config.deep_watch_limit = min(config.deep_watch_limit, limit)
            uvicorn.run(
                create_app(settings, trade_alert_settings=alert_config, pre_ignition_settings=pre_ignition_config),
                host=args.host, port=args.port, log_level=args.log_level.lower(),
                timeout_graceful_shutdown=10,
            )
        elif args.command == "trade-alert-test":
            import httpx

            from .notification_runtime import TelegramSettings, send_telegram_test
            async def check():
                config = TelegramSettings.from_env()
                async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                    return await send_telegram_test(client, config)
            print(json.dumps(asyncio.run(check()), default=str))
        elif args.command == "trade-alert-status":
            from .intelligence_store import IntelligenceStore
            from .models import utcnow
            store = IntelligenceStore(settings.db_path)
            try:
                state = store.get_state("trade_alert_runtime", "status")
                print(json.dumps({"database": str(Path(settings.db_path).resolve()),
                    "queried_at": utcnow().isoformat(), "status": state or "UNAVAILABLE",
                    "scope": "Persisted status; source timestamp/age determines freshness"}, default=str))
            finally:
                store.close()
        elif args.command == "trade-alert-replay":
            from .trade_alert_models import TradeAlertSettings
            from .trade_alert_research import replay_trade_alerts
            report = replay_trade_alerts(Path(args.input),
                TradeAlertSettings.model_validate_json(Path(args.alert_config).read_text()))
            rendered = json.dumps(report, indent=2, default=str, allow_nan=False)
            if args.output:
                Path(args.output).write_text(rendered + "\n")
            else:
                print(rendered)
        elif args.command in {"live-monitor", "live-smoke"}:
            from .live_runtime import LiveObservationWorker, live_smoke
            if args.seconds is not None and (args.seconds <= 0 or args.seconds > 86400):
                raise ValueError("seconds must be positive and at most 86400")
            if args.command == "live-smoke":
                write_output(asyncio.run(live_smoke(args.symbol or ["BTCUSDT"], seconds=args.seconds)), args.output)
            else:
                worker = LiveObservationWorker(settings, symbols=args.symbol)
                write_output(asyncio.run(worker.run(seconds=args.seconds)), args.output)
        elif args.command in {"watchlist", "intelligence", "macro-import", "macro-calendar"}:
            from .intelligence import IntelligenceWorker, import_macro
            from .intelligence_store import IntelligenceStore
            store = IntelligenceStore(settings.db_path)
            try:
                if args.command == "watchlist":
                    if args.action == "add":
                        if not args.symbol:
                            raise ValueError("add requires --symbol e.g. XRPUSDT")
                        from .live_models import normalize_symbols
                        symbol = normalize_symbols([args.symbol])[0]
                        store.watchlist_add(args.asset_id or f"exchange:binance:{symbol}", symbol,
                            priority=args.priority, pinned=args.pinned, notes=args.notes)
                    elif args.action == "remove":
                        if not args.asset_id:
                            raise ValueError("remove requires --asset-id")
                        store.watchlist_remove(args.asset_id)
                    write_output({"watchlist": store.watchlist(), "database_path": store.database_path}, args.output)
                elif args.command == "intelligence":
                    from .event_learning import EventLearningEngine
                    from .models import utcnow
                    if args.interval < 1:
                        raise ValueError("worker interval must be at least one second")
                    async def intelligence_loop():
                        worker = IntelligenceWorker(store)
                        while True:
                            report = await worker.run_once()
                            report["outcomes"] = await asyncio.to_thread(EventLearningEngine(store).evaluate, utcnow())
                            write_output(report, args.output)
                            if not args.watch:
                                return
                            await asyncio.sleep(args.interval)
                    if args.watch:
                        asyncio.run(intelligence_loop())
                    else:
                        # A one-shot offline worker has no concurrent market
                        # tasks. Run directly; the live feed remains a separate
                        # process and no idle cross-thread event-loop wakeup is
                        # needed to publish completed research outcomes.
                        report = IntelligenceWorker(store).process_latest()
                        report["outcomes"] = EventLearningEngine(store).evaluate(utcnow())
                        write_output(report, args.output)
                elif args.command == "macro-import":
                    from .macro_models import MacroProviderReport
                    report = MacroProviderReport.model_validate_json(Path(args.file).read_text())
                    write_output(import_macro(store, report), args.output)
                else:
                    from datetime import timedelta

                    from .models import utcnow
                    from .providers.macro import BLSCalendarProvider
                    if not 1 <= args.days <= 366:
                        raise ValueError("calendar days must be 1..366")
                    async def calendar_fetch():
                        async with BLSCalendarProvider() as provider:
                            return await provider.calendar(utcnow(), utcnow()+timedelta(days=args.days))
                    report = asyncio.run(calendar_fetch())
                    write_output({"provider": report.model_dump(mode="json"), "stored": import_macro(store, report)}, args.output)
            finally:
                store.close()
        elif args.command == "research":
            from .research import ResearchSettings, StudyDataset, run_study

            dataset = StudyDataset.model_validate_json(Path(args.dataset).read_text())
            research_settings = (
                ResearchSettings.model_validate_json(Path(args.research_config).read_text())
                if args.research_config
                else ResearchSettings()
            )
            write_output(run_study(dataset, research_settings), args.output)
        elif args.command == "research-schema":
            from .research import StudyDataset

            write_output(StudyDataset.model_json_schema(), args.output)
    except KeyboardInterrupt:
        print("Stopped; unfinished runs remain unpublished.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        logging.error("%s: %s", type(exc).__name__, exc)
        if args.log_level == "DEBUG":
            logging.exception("Failure detail")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
