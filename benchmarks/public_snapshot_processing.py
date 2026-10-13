"""Process genuine public snapshots captured by the connected Binance adapter.

This explicitly tests REST snapshots through production contracts and the app
database/API. It is not a WebSocket or uptime test. Source availability is the
actual acquisition/persistence boundary, never backdated to candle event times.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from expansion_hunter.fast import FastIgnitionMonitor
from expansion_hunter.intelligence_store import IntelligenceStore
from expansion_hunter.live_analysis import analyze_live
from expansion_hunter.live_feed import LiveFeedState
from expansion_hunter.live_models import DepthLevel, LiveFeedSettings, LiveMarketEvent
from expansion_hunter.models import Asset, AssetSnapshot, Candle


def run(source: str, database: str):
    data = json.loads(Path(source).read_text())
    boundary = datetime.strptime(data["retrieved_at"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
    symbol = data["symbol"]
    asset = Asset(asset_id="bitcoin", symbol="BTC", name="Bitcoin", exchange_symbol=symbol)
    state = LiveFeedState(LiveFeedSettings(timeframes=["1m", "5m", "15m", "1h", "4h", "1D"]))
    rejected_open = 0
    for frame, rows in data["native_klines"].items():
        bars = []
        for row in rows:
            end = datetime.fromtimestamp((int(row[6])+1)/1000, UTC)
            if end > boundary:
                rejected_open += 1
                continue
            bars.append(Candle(open_time=datetime.fromtimestamp(int(row[0])/1000, UTC), close_time=end,
                available_at=boundary, open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[5]), quote_volume=float(row[7]),
                taker_buy_volume=float(row[9]), source="binance:spot:public-native-REST:connected-adapter"))
        state.seed_candles(symbol, "1D" if frame=="1d" else frame, bars, boundary)
    book = data["native_book"]
    event = LiveMarketEvent(symbol=symbol, kind="DEPTH", source="binance:spot:public-native-REST:connected-adapter",
        received_timestamp=boundary, processed_timestamp=boundary, sequence_id=book["lastUpdateId"],
        bids=[DepthLevel(price=float(p),quantity=float(q)) for p,q in book["bids"]],
        asks=[DepthLevel(price=float(p),quantity=float(q)) for p,q in book["asks"]], depth_scope="TOP_20_SNAPSHOT")
    state.apply(event)
    live = state.snapshot(symbol, boundary)
    snapshot = AssetSnapshot(asset=asset, as_of=boundary,
                             candles={tf:state.candles(symbol,tf) for tf in ("1m","5m","15m","1h","4h","1D")})
    analysis = analyze_live(snapshot,event)
    live.update(analysis)
    live["asset_id"] = asset.asset_id
    live["received_timestamp"] = boundary.isoformat()
    live["price"] = (event.bids[0].price+event.asks[0].price)/2
    live["source_mode"] = "REAL_PUBLIC_REST_SNAPSHOT; NOT A CONTINUOUS FEED"
    result = FastIgnitionMonitor().evaluate(asset,snapshot,evaluation_at=boundary)
    store = IntelligenceStore(database)
    try:
        store.put_state("live_asset",symbol,boundary,live)
        store.put_state("fast_state",asset.asset_id,boundary,result.state)
        receipt = {"test":"REAL_PUBLIC_REST_TO_PRODUCTION_PROCESSING", "boundary":boundary.isoformat(),
            "database_path":store.database_path,
            "closed_bars":{tf:len(bars) for tf,bars in snapshot.candles.items()},
            "unfinished_candles_excluded":rejected_open,
            "native_book_levels":len(event.bids)+len(event.asks),
            "exact_native_best_bid":event.bids[0].price,"exact_native_best_ask":event.asks[0].price,
            "overlays":len(analysis.get("overlays",[])),"state":result.state.stage.value,
            "websocket_verified":False,"continuous_connectivity_claim":False,
            "future_feature_timestamps":any(b.available_at>boundary or b.close_time>boundary
                                             for bars in snapshot.candles.values() for b in bars)}
        return receipt
    finally:
        store.close()


if __name__ == "__main__":
    receipt=run("reports/public-market-smoke.json","data/cockpit.sqlite3")
    Path("reports/public-snapshot-processing.json").write_text(json.dumps(receipt,indent=2)+"\n")
    print(json.dumps(receipt,indent=2))
