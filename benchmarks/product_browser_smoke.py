"""Real HBAR browser acceptance. No synthetic candles, identities or signals."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright


async def run(base: str, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {"test": "REAL_PUBLIC_HBAR_PRODUCT_BROWSER", "synthetic_fallback": False,
              "live_trading": False, "checks": {}, "requests": [], "screenshots": []}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=shutil.which("chromium"),
                                                   args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def request(path, method="GET"):
            start = time.perf_counter()
            response = await page.request.fetch(base + path, method=method, timeout=25000)
            result["requests"].append({"path": path, "status": response.status,
                                       "milliseconds": (time.perf_counter() - start) * 1000})
            assert response.ok, f"{path}: HTTP {response.status}"
            return await response.json()

        async def fresh(timeout=240):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = await request("/api/live/assets/HBARUSDT")
                live = value.get("live", {})
                if value["status"] == "AVAILABLE" and all(len(live.get("closed_candles", {}).get(f, [])) >= 32
                        for f in ("1m", "5m", "15m", "1h", "4h", "1D")):
                    return value
                await page.wait_for_timeout(3000)
            raise AssertionError("HBAR did not receive fresh six-timeframe public history")

        stopped = False
        try:
            assert not (await request("/api/watchlist"))["watchlist"]
            await page.goto(base + "/cockpit", wait_until="domcontentloaded")
            await page.wait_for_function("Number.parseInt(document.querySelector('#search-count').textContent, 10) >= 500", timeout=60000)
            assert await page.locator("html").get_attribute("lang") == "en"
            assert await page.locator("body").get_attribute("data-mode") == "rookie"
            await page.locator("#global-search").fill("HBAR")
            choice = page.locator('#search-results [data-symbol="HBARUSDT"]')
            await choice.wait_for(timeout=10000)
            await choice.click()
            # Renewal is a chart subscription, never a watchlist or scoring change.
            # It also refreshes an already archived but currently demoted cache.
            await request("/api/live/chart-interest/HBARUSDT", "POST")
            live = await fresh()
            await page.wait_for_function("document.querySelector('#chart rect') !== null", timeout=30000)
            result["checks"]["search_hbar_and_native_history"] = True
            result["provider"] = {"price_source": live["live"].get("price_source"),
                                  "received_timestamp": live["live"].get("received_timestamp")}
            result["timeframes"] = {}
            for frame in ("1m", "5m", "15m", "1h", "4h", "1D"):
                await page.locator(f'[data-frame="{frame}"]').click()
                await page.wait_for_timeout(200)
                candles = await page.locator("#chart rect").count()
                assert candles >= 20, frame
                fib = live["live"].get("active_fibonacci", live["live"].get("analysis", {}).get("active_fibonacci", {})).get(frame)
                assert fib is not None and fib["timeframe"] == frame, frame
                result["timeframes"][frame] = {"chart_rectangles": candles, "fib": fib}
            result["checks"]["six_own_timeframe_fibonacci_calculations"] = True
            await page.locator('[data-frame="1m"]').click()
            before = await page.locator("#chart").inner_html()
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                await page.wait_for_timeout(4000)
                updated = await request("/api/live/assets/HBARUSDT")
                if updated["status"] == "AVAILABLE" and before != await page.locator("#chart").inner_html():
                    result["checks"]["chart_changes_without_page_refresh"] = True
                    break
            assert result["checks"].get("chart_changes_without_page_refresh")
            native = [level for level in live["live"].get("liquidity_map", []) if level.get("category") == "OBSERVED_ORDER_BOOK"]
            assert native, "No observed order book: cannot claim live liquidity"
            result["observed_liquidity_examples"] = native[:4]
            for width, height, name in ((1440, 1000, "desktop"), (820, 1180, "tablet"), (390, 844, "mobile")):
                await page.set_viewport_size({"width": width, "height": height})
                await page.wait_for_timeout(300)
                assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), name
                await page.locator("#live-chart").scroll_into_view_if_needed()
                image_path = output.parent / f"product-{name}.png"
                await page.screenshot(path=str(image_path), full_page=False)
                result["screenshots"].append(str(image_path))
            result["checks"]["mobile_tablet_desktop_no_horizontal_overflow"] = True
            await request("/api/live/stop", "POST")
            stopped = True
            await page.wait_for_function("document.querySelector('#live-button').dataset.running === 'false'", timeout=35000)
            result["checks"]["disconnect_visible"] = await page.locator("#feed-label").inner_text()
            await request("/api/live/start", "POST")
            stopped = False
            await request("/api/live/chart-interest/HBARUSDT", "POST")
            await fresh()
            result["checks"]["reconnect_fresh_history"] = True
            result["checks"]["watchlist_stays_empty"] = not (await request("/api/watchlist"))["watchlist"]
            assert result["checks"]["watchlist_stays_empty"]
            result["final_feed_health"] = (await request("/api/live/health"))["feed_health"]
            result["browser_errors"] = errors
            assert not errors, errors
            result["status"] = "PASS"
            result["limitations"] = ["Short observed smoke, not 24/7 endurance proof", "Fib can be invalidated/unavailable; no hindsight anchor invented",
                                      "No credentials for wallet provider or external push delivery were supplied"]
        except BaseException as error:
            result["status"] = "FAIL"
            result["error"] = f"{type(error).__name__}: {error}"
            await page.screenshot(path=str(output.parent / "product-smoke-failure.png"), full_page=True)
            raise
        finally:
            result["browser_errors"] = errors
            if stopped:
                await request("/api/live/start", "POST")
            result["completed_at"] = datetime.now(UTC).isoformat()
            output.write_text(json.dumps(result, indent=2, allow_nan=False, default=str) + "\n")
            await browser.close()
    print(json.dumps({"status": result["status"], "report": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("reports/product-browser-smoke.json"))
    args = parser.parse_args()
    asyncio.run(run(args.base, args.output))
