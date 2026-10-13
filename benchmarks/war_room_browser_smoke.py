"""Real public-feed browser acceptance, with no injected assets or prices.

Requires a running local observation app and an empty personal watchlist. A
natural candidate demotion is recorded; selection uses current automatic rows.
Synthetic regression tests separately cover deterministic ignition outcomes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright


async def run(base: str, destination: Path):
    evidence = {"test": "REAL_PUBLIC_AUTOMATIC_WAR_ROOM_BROWSER", "base_url": base,
                "synthetic_fallback": False, "manually_added_assets": [], "live_trading": False,
                "checks": {}, "candidate_changes": [], "api_latencies_ms": []}
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, executable_path=shutil.which("chromium"), args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def request(path, *, method="GET"):
            start = time.perf_counter()
            response = await page.request.fetch(base + path, method=method, timeout=15000)
            evidence["api_latencies_ms"].append({"path": path, "status": response.status,
                                                "ms": (time.perf_counter() - start) * 1000})
            assert response.ok, f"{path}: HTTP {response.status}"
            return await response.json()

        async def ready(timeout=180):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                health = await request("/api/live/health")
                if health["ready"] and health["broad_running"]:
                    return health
                await page.wait_for_timeout(3000)
            raise AssertionError("No fresh automatic fast-watch evaluation within readiness budget")

        stopped = False
        try:
            await page.goto(base + "/cockpit", wait_until="domcontentloaded")
            assert not (await request("/api/watchlist"))["watchlist"], "User watchlist must not be changed by smoke"
            health = await request("/api/live/health")
            if not health["runtime"]["running"]:
                await request("/api/live/start", method="POST")
            health = await ready()
            assert health["discovery_runtime"]["configured_assets"] == 500
            evidence["checks"]["automatic_start_ready"] = True
            evidence["provider_health_before"] = health["feed_health"]
            selected = None
            for _selection in range(3):
                overview = await request("/api/cockpit/overview")
                candidates = sorted((row for row in overview["assets"] if row.get("eligible")
                                     and row.get("live_status") == "AVAILABLE"
                                     and row.get("state") not in {"DORMANT", "DEMOTED", "INVALIDATED"}),
                                    key=lambda row: row.get("rank", {}).get("current_rank") or 10000)[:50]
                for row in candidates:
                    symbol = row.get("exchange_symbol")
                    response = await page.request.get(base + "/api/live/assets/" + symbol, timeout=15000)
                    if not response.ok:
                        continue
                    value = await response.json()
                    live = value.get("live", {})
                    closed = live.get("closed_candles", {})
                    levels = live.get("liquidity_map", [])
                    if (value.get("status") != "AVAILABLE" or live.get("price_quality") != "AVAILABLE"
                            or not all(len(closed.get(frame, [])) >= 32 for frame in ("1m", "5m", "15m", "1h", "4h", "1D"))
                            or not any(level.get("category") == "OBSERVED_ORDER_BOOK" for level in levels)):
                        continue
                    locator = page.locator('#discovery-body [data-symbol="' + symbol + '"]')
                    if await locator.count() == 0:
                        continue
                    await locator.click()
                    await page.locator('[data-frame="1m"]').click()
                    await page.wait_for_function("document.querySelector('#chart rect') !== null", timeout=20000)
                    before = await page.locator("#chart").inner_html()
                    for _ in range(14):
                        await page.wait_for_timeout(5000)
                        current = await request("/api/live/assets/" + symbol)
                        if current["status"] != "AVAILABLE":
                            evidence["candidate_changes"].append({"symbol": symbol, "status": current["status"]})
                            break
                        if (before != await page.locator("#chart").inner_html()
                                and live["received_timestamp"] < current["live"]["received_timestamp"]):
                            selected = symbol, current
                            break
                    if selected:
                        break
                if selected:
                    break
                await page.wait_for_timeout(3000)
            assert selected, "No automatically promoted coin demonstrated a fresh moving chart"
            symbol, current = selected
            evidence["selected_automatic_symbol"] = symbol
            evidence["checks"]["live_chart_without_refresh"] = True
            evidence["checks"]["native_current_price"] = current["live"]["price"]
            evidence["timeframe_candle_rectangles"] = {}
            for frame in ("1m", "5m", "15m", "1h", "4h", "1D"):
                await page.locator('[data-frame="' + frame + '"]').click()
                count = await page.locator("#chart rect").count()
                assert count > 20, frame
                evidence["timeframe_candle_rectangles"][frame] = count
            native = [level for level in current["live"]["liquidity_map"]
                      if level.get("category") == "OBSERVED_ORDER_BOOK"]
            assert native
            evidence["native_liquidity_price_examples"] = [{key: level.get(key) for key in
                ("price", "size", "side", "status", "source", "data_quality")} for level in native[:4]]
            await page.screenshot(path=str(destination.parent / "war-room-desktop.png"), full_page=True)
            mobile = await browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            await mobile.goto(base + "/cockpit", wait_until="domcontentloaded")
            await mobile.wait_for_timeout(4000)
            target = mobile.locator('#discovery-body [data-symbol="' + symbol + '"]')
            if await target.count():
                await target.click()
            await mobile.wait_for_function("document.querySelector('#chart rect') !== null", timeout=20000)
            assert not await mobile.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
            await mobile.screenshot(path=str(destination.parent / "war-room-mobile.png"), full_page=True)
            evidence["checks"]["mobile_no_page_overflow"] = True
            await mobile.close()
            await request("/api/live/stop", method="POST")
            stopped = True
            await page.wait_for_function("document.querySelector('#live-button').dataset.running === 'false'", timeout=25000)
            evidence["checks"]["stopped_connection_visible"] = await page.locator("#feed-label").inner_text()
            await request("/api/live/start", method="POST")
            stopped = False
            final = await ready()
            evidence["checks"]["reconnect_ready"] = True
            evidence["provider_health_after"] = final["feed_health"]
            evidence["actual_latest_cycle_counts"] = final["broad_counts"]
            evidence["live_processing_metrics"] = final["metrics"]
            evidence["browser_errors"] = errors
            assert not errors, errors
            assert not (await request("/api/watchlist"))["watchlist"]
            evidence["checks"]["empty_personal_watchlist_preserved"] = True
            evidence["status"] = "PASS"
            evidence["limitations"] = ["Short smoke, not 24/7 endurance or full500 provider coverage proof",
                                       "No external notification credentials or predictive edge tested",
                                       "Current universe coverage and intensive fast monitoring differ"]
        except BaseException as error:
            evidence["status"] = "FAIL"
            evidence["error"] = f"{type(error).__name__}: {error}"
            await page.screenshot(path=str(destination.parent / "war-room-smoke-failure.png"), full_page=True)
            raise
        finally:
            if stopped:
                await request("/api/live/start", method="POST")
            evidence["completed_at"] = datetime.now(UTC).isoformat()
            destination.write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n")
            await browser.close()
    print(destination, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("reports/war-room-browser-smoke.json"))
    options = parser.parse_args()
    asyncio.run(run(options.base, options.output))
