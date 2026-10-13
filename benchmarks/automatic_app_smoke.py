"""Real automatic TOP500 -> promoted public feeds -> browser smoke.

Requires an already-running local app and an EMPTY personal watchlist. No
symbols or market values are injected. Short smoke, not 24/7 validation.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright


async def run(base="http://127.0.0.1:8000"):
    output = Path("reports")
    output.mkdir(exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, executable_path=shutil.which("chromium"), args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(base + "/cockpit", wait_until="domcontentloaded")
        personal = await (await page.request.get(base + "/api/watchlist")).json()
        assert not personal["watchlist"], "Test requires no personal watchlist; never removes user records"
        await page.wait_for_function("document.querySelector('#live-button').disabled === false")
        if await page.locator("#live-button").get_attribute("data-running") != "true":
            await page.locator("#live-button").click()
        ready = None
        for attempt in range(120):
            await page.wait_for_timeout(1000)
            health = await (await page.request.get(base + "/api/live/health")).json()
            if health["ready"] and health["broad_running"]:
                ready = health
                break
            if attempt % 10 == 0:
                print(json.dumps({"waiting": attempt, "broad_running": health["broad_running"],
                                  "feed_fresh": health["feed_fresh"], "fast_assets": health["fresh_fast_assets"]}), flush=True)
        assert ready, "Automatic broad scanner and native fast feed did not become ready"
        assert ready["discovery_runtime"]["configured_assets"] == 500
        assert not ready["live_trading"]
        overview = await (await page.request.get(base + "/api/cockpit/overview")).json()
        candidates = [row for row in overview["assets"] if row.get("eligible") and
                      row.get("state") not in ("DORMANT", "INVALIDATED") and row.get("exchange_symbol")]
        assert candidates, "No actual automatic promotion exists"
        selected = None
        for _attempt in range(120):
            for row in candidates:
                response = await page.request.get(base + "/api/live/assets/" + row["exchange_symbol"])
                if response.status != 200:
                    continue
                value = await response.json()
                if value["status"] == "AVAILABLE" and value["live"].get("overlays"):
                    selected = row, value
                    break
            if selected:
                break
            await page.wait_for_timeout(1000)
        assert selected, "Promoted asset has not received fresh native candles and overlays"
        row, first = selected
        symbol = row["exchange_symbol"]
        await page.locator('#discovery-body [data-symbol="' + symbol + '"]').click()
        await page.locator('[data-frame="1m"]').click()
        await page.wait_for_function("document.querySelector('#chart rect') !== null", timeout=25000)
        before_chart = await page.locator("#chart").inner_html()
        # A genuine thin market can have unchanged prices for five seconds.
        # Allow a native 1m boundary; never inject a tick to force the result.
        visual_wait = 0
        for _ in range(14):
            await page.wait_for_timeout(5000)
            visual_wait += 5
            if before_chart != await page.locator("#chart").inner_html():
                break
        second = await (await page.request.get(base + "/api/live/assets/" + symbol)).json()
        assert second["status"] == "AVAILABLE"
        assert first["live"]["received_timestamp"] < second["live"]["received_timestamp"]
        assert before_chart != await page.locator("#chart").inner_html()
        native = [level for level in second["live"]["liquidity_map"]
                  if level.get("category") == "OBSERVED_ORDER_BOOK" and
                  level.get("status") in ("RESTING", "APPROACHING", "TESTING")]
        assert native, "No actual resting order-book price levels"
        frames = {}
        for frame in ("1m", "5m", "15m", "1h", "4h", "1D"):
            await page.locator('[data-frame="' + frame + '"]').click()
            frames[frame] = await page.locator("#chart rect").count()
            assert frames[frame] > 20, "No real candle history for " + frame
        await page.locator('[data-frame="5m"]').click()
        await page.screenshot(path=str(output / "automatic-cockpit-desktop.png"), full_page=True)
        mobile = await browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        await mobile.goto(base + "/cockpit", wait_until="domcontentloaded")
        await mobile.wait_for_timeout(4000)
        await mobile.locator('#discovery-body [data-symbol="' + symbol + '"]').click()
        await mobile.wait_for_function("document.querySelector('#chart rect') !== null", timeout=25000)
        assert not await mobile.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
        await mobile.screenshot(path=str(output / "automatic-cockpit-mobile.png"), full_page=True)
        assert not (await (await page.request.get(base + "/api/watchlist")).json())["watchlist"]
        assert not errors, errors
        final = await (await page.request.get(base + "/api/live/health")).json()
        report = {"test": "REAL_AUTOMATIC_TOP500_APP_BROWSER", "completed_at": datetime.now(UTC).isoformat(),
                  "base_url": base, "personal_watchlist_count": 0, "manually_injected_symbols": [],
                  "configured_universe": 500, "actual_broad_counts": final["broad_counts"],
                  "broad_process": final["discovery_runtime"], "automatic_candidates": len(candidates),
                  "fresh_fast_assets": final["fresh_fast_assets"], "selected_automatic_symbol": symbol,
                  "native_source": "BINANCE_PUBLIC_WEBSOCKET", "synthetic_market_fallback": False,
                  "chart_updates_without_refresh": True, "timeframe_rectangles": frames,
                  "seconds_until_observed_visual_change": visual_wait,
                  "current_native_liquidity_levels": len(native), "overlays": len(second["live"]["overlays"]),
                  "received_before": first["live"]["received_timestamp"],
                  "received_after": second["live"]["received_timestamp"], "browser_errors": errors,
                  "mobile_no_horizontal_overflow": True, "live_trading": False,
                  "limits": ["short public-data smoke, not endurance proof", "provider coverage is incomplete",
                             "broad universe coverage is different from intensive live monitoring",
                             "no predictive performance or notification delivery established"]}
        (output / "automatic-app-smoke.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
