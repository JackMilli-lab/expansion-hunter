# Expansion Hunter web deployment

This release contains the existing Python observation app and its browser cockpit. It contains **no database, credentials, dependency environment, desktop installer or test data**. It does not enable exchange trading or deploy a service automatically.

## Docker on an always-on machine

Extract the archive, open its `ExpansionHunter-Web-Production` folder and use the existing Docker configuration:

```bash
export COCKPIT_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up --build -d
```

Keep this token in the host's secret management configuration so it remains available for later restarts. Do not commit it. The service starts with:

```bash
expansion-hunter app --host 0.0.0.0 --port 8000
```

The existing compose file exposes the service only on the host's loopback interface. Open `http://127.0.0.1:8000/cockpit` on that machine. For access from your phone or another computer, put an authenticated HTTPS reverse proxy in front of it; do not expose port 8000 directly. Configure the dashboard's token field with the same secret.

Docker named volumes preserve the SQLite database and reports across ordinary container restarts. Do not run `docker compose down --volumes` unless deliberately deleting those data volumes. Maintain separate backups of the live data, using SQLite's backup API or another WAL-aware backup method. Copying only the main SQLite file during active writes can omit committed WAL data.

## Render blueprint

`render.yaml` defines a **paid** native-Python web service with a 1 GiB persistent disk. Service/disk and optional data-provider charges are your responsibility; no charges are incurred by downloading this blueprint. Review Render's current pricing and region/provider access before creating it. The free tier cannot supply this blueprint's durable disk or a credible always-on deployment.

Upload the extracted source to a repository you control, then create a Render Blueprint from that repository. Render supplies TLS and the `PORT` environment variable. The blueprint generates `COCKPIT_API_TOKEN`; retrieve it privately in Render's environment settings and enter it in the cockpit's token field. The startup wrapper refuses to start without a token or the `/var/data` mount. It changes only the runtime database path, preserving existing application/configuration logic.

Build command:

```bash
pip install -r requirements-runtime.txt && pip install --no-deps .
```

Start command:

```bash
python deploy/start-render.py
```

The application binds `0.0.0.0:$PORT` and stores SQLite at `/var/data/cockpit.sqlite3`. Run **one app process/service instance** for this SQLite-backed deployment. Do not horizontally scale shared ingestion against one SQLite file. Archive growth needs monitoring; increase disk size or add a separately reviewed retention policy before space is exhausted.

## Credentials and health

No private exchange trading keys are needed. `COCKPIT_API_TOKEN` protects app data and controls. Optional public-data/notification variables are documented in the included README; do not include them in source. Do not configure Telegram delivery until you want alerts sent to the chosen account.

`/health` is the deployment process/operational health check. `/ready` and cockpit Data Health expose data readiness. A reachable HTTP server does not prove reachable Binance/CoinGecko feeds, fresh coverage, predictive edge or 24/7 reliability. Inspect provider status after deployment and verify actual public-market updates in the chosen host region. No hosting or real-source deployment smoke test has been performed for this archive.

The runtime requirements pin direct production packages recorded by the project. Transitive dependencies and base images are not fully locked; this is a source release, not a prebuilt or independently endurance-tested container.
