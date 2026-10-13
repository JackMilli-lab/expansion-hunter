"""Real browser -> app -> public Binance smoke; no synthetic market fallback.

Run against an already-running local cockpit with a BTCUSDT watchlist member.
Stops/restarts the observation feed to verify visible disconnect recovery.
Browser package/binary are optional development dependencies, not app runtime.
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
        browser = await playwright.chromium.launch(headless=True, executable_path=shutil.which("chromium"),
                                                    args=["--no-sandbox"])
        desktop = await browser.new_page(viewport={"width": 1440, "height": 1080}, device_scale_factor=1)
        errors = []
        desktop.on("pageerror", lambda error: errors.append(str(error)))
        await desktop.goto(base + "/cockpit", wait_until="domcontentloaded")
        await desktop.wait_for_function("document.querySelector('#chart rect') !== null", timeout=25000)
        first = await (await desktop.request.get(base + "/api/live/assets/BTCUSDT")).json()
        initial_chart = await desktop.locator("#chart").inner_html()
        await desktop.wait_for_timeout(5000)
        second = await (await desktop.request.get(base + "/api/live/assets/BTCUSDT")).json()
        updated_chart = await desktop.locator("#chart").inner_html()
        health = await (await desktop.request.get(base + "/api/live/health")).json()
        assert first["status"] == second["status"] == "AVAILABLE", "native feed is not fresh"
        assert health["feed_fresh"] and health["ready"], "application is not ready"
        assert first["live"]["received_timestamp"] < second["live"]["received_timestamp"]
        assert initial_chart != updated_chart, "chart did not update without refresh"
        assert second["live"].get("overlays"), "real indicator overlays missing"
        native = [level for level in second["live"]["liquidity_map"]
                  if level.get("category") == "OBSERVED_ORDER_BOOK"]
        assert native, "native resting liquidity levels missing"
        stored = second["live"].get("scores", {}).get("stored_energy", {}).get("value")
        if stored is not None:
            assert "Stored Energy" in await desktop.locator("#score-rows").inner_text()
            assert await desktop.locator("#score-rows .score-fill").first.get_attribute("style") != "width:0%"
        # Exercise the real strict watchlist API through its browser pin handler.
        before_watch = await (await desktop.request.get(base + "/api/watchlist")).json()
        bitcoin = next(item for item in before_watch["watchlist"] if item["symbol"] == "BTCUSDT")
        pin = desktop.locator('[data-pin="' + bitcoin["asset_id"] + '"]')
        await pin.click()
        await desktop.wait_for_timeout(1500)
        after_pin = await (await desktop.request.get(base + "/api/watchlist")).json()
        changed = next(item for item in after_pin["watchlist"] if item["asset_id"] == bitcoin["asset_id"])
        assert bool(changed["pinned"]) != bool(bitcoin["pinned"]), "pin request failed"
        await desktop.locator('[data-pin="' + bitcoin["asset_id"] + '"]').click()
        await desktop.wait_for_timeout(1000)
        frames = {}
        for frame in ("1m", "5m", "15m", "1h", "4h", "1D"):
            await desktop.locator(f'[data-frame="{frame}"]').click()
            frames[frame] = await desktop.locator("#chart rect").count()
            assert frames[frame] > 20, f"no real candles for {frame}"
        await desktop.locator('[data-frame="5m"]').click()
        overlay_text = await desktop.locator("#chart").text_content()
        assert "VWAP" in overlay_text, "real VWAP overlay is not visible"
        await desktop.screenshot(path=str(output / "cockpit-desktop.png"), full_page=True)

        mobile = await browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        await mobile.goto(base + "/cockpit", wait_until="domcontentloaded")
        await mobile.wait_for_function("document.querySelector('#chart rect') !== null", timeout=25000)
        overflow = await mobile.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
        assert not overflow, "mobile page has horizontal overflow"
        await mobile.screenshot(path=str(output / "cockpit-mobile.png"), full_page=True)

        await desktop.request.post(base + "/api/live/stop", data={})
        await desktop.wait_for_function("document.querySelector('#live-button').dataset.running === 'false'", timeout=12000)
        stopped_label = await desktop.locator("#feed-label").inner_text()
        stopped = await (await desktop.request.get(base + "/api/live/health")).json()
        assert not stopped["feed_fresh"], "stopped feed still classified fresh"
        assert not stopped["runtime"]["running"]
        await desktop.screenshot(path=str(output / "cockpit-disconnected.png"), full_page=True)
        await desktop.request.post(base + "/api/live/start", data={})
        for _ in range(15):
            await desktop.wait_for_timeout(1000)
            recovered = await (await desktop.request.get(base + "/api/live/health")).json()
            if recovered["feed_fresh"]:
                break
        assert recovered["feed_fresh"], "real public feed did not reconnect"
        assert recovered["runtime"]["running"]
        await desktop.wait_for_timeout(3000)
        assert not errors, f"browser JavaScript errors: {errors}"
        report = {"test": "REAL_PUBLIC_APP_BROWSER", "completed_at": datetime.now(UTC).isoformat(),
                  "base_url": base, "native_market_source": "BINANCE_PUBLIC_WEBSOCKET",
                  "synthetic_fallback": False, "chart_updates_without_refresh": True,
                  "fresh_application": health["ready"], "timeframe_rectangles": frames,
                  "exact_native_liquidity_levels": len(native), "indicator_overlays": len(second["live"]["overlays"]),
                  "received_before": first["live"]["received_timestamp"],
                  "received_after": second["live"]["received_timestamp"],
                  "price_before": first["live"]["price"], "price_after": second["live"]["price"],
                  "mobile_horizontal_overflow": overflow, "stopped_ui_label": stopped_label,
                  "disconnect_visible": True, "reconnected_public_feed": recovered["feed_fresh"],
                  "browser_errors": errors, "screenshots": ["cockpit-desktop.png", "cockpit-mobile.png", "cockpit-disconnected.png"],
                  "watchlist_pin_browser_contract": "PASS", "native_indicator_labels_visible": "VWAP",
                  "limits": ["short smoke, not 24/7 endurance", "one real symbol, not 500-asset coverage",
                             "provider interruptions exercised via app stop/start, not network packet loss",
                             "does not manufacture or require a real market breakout during the test"]}
        (output / "live-app-smoke.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
