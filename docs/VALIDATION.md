# Validation record

Validated in this workspace on 2026-10-03 with Python 3.12.14. Direct library/tool versions are recorded in `requirements-tested.txt`.

- Full test suite: **201 passed in 13.49 seconds**.
- Ruff checks and formatting checks passed.
- Editable package installation and installed CLI entry point verified.
- Research settings and input JSON schema exported and parsed successfully.
- Installed research CLI rejected the intentionally empty historical template; it did not create fabricated study results.
- Portable Python wheel built locally without fetching or fabricating market data.

The 25 research tests use explicitly synthetic observations and the actual technical analyzer, including 56 contemporaneous assets and 50 selected controls. They verify that future outcome changes and future bars do not alter pre-event matching, that other winners remain in the control group, that gaps and unavailable labels are censored/purged, and that future-unavailable metadata cannot establish features. They also check normalized funding, genuine-adoption filters, score error denominators, frozen matching scales and separated holdout comparison.

Operational and technical tests cover original candle knowledge versus revisions, quote/base consistency, native/estimated AVWAP prefix calculations, confirmed swing/anchor discovery, breakout acceptance, source collisions, missing/stale data, bounded HTTP failures, liquidity/account limits, candidate refresh, interrupted publication and read-only authenticated API behavior.

Real-source integration was checked separately: CoinGecko metadata/global data and Binance public spot data were retrieved successfully. Default Binance trading and USD-M futures hosts returned HTTP 451 in this environment; the scan reported their unavailability and emitted conservative decisions. No exchange orders were sent. Full-500 live throughput and predictive strategy performance were not established by this smoke scan.

No historical QNT/ZEC matched-winner performance claims have been produced. A trustworthy historical point-in-time universe and original pre-event data, including assets that later disappeared, are required to run that substantive study. Provenance fields are operator attestations and must be backed by source artifacts.
