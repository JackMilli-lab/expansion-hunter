"""Start the existing observation app with Render's port and persistent disk."""

import json
import os
from pathlib import Path


def main():
    port = int(os.environ.get("PORT", "10000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    if not os.environ.get("COCKPIT_API_TOKEN"):
        raise RuntimeError("COCKPIT_API_TOKEN is required for a remotely hosted cockpit")
    data = Path("/var/data")
    if not data.is_dir():
        raise RuntimeError("Persistent disk /var/data is not mounted")
    settings = json.loads(Path("config/settings.json").read_text())
    settings["db_path"] = str(data / "cockpit.sqlite3")
    runtime_config = Path("/tmp/expansion-hunter-render-settings.json")
    runtime_config.write_text(json.dumps(settings))
    os.execvp("expansion-hunter", [
        "expansion-hunter", "--config", str(runtime_config), "app",
        "--host", "0.0.0.0", "--port", str(port),
    ])


if __name__ == "__main__":
    main()
