#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
if [[ ! -x .app-venv/bin/python ]]; then
  python3 -m venv .app-venv
fi
.app-venv/bin/python -m pip install .
printf 'Expansion Hunter: http://127.0.0.1:8000/cockpit\nPublic market data only; no exchange orders.\n'
exec .app-venv/bin/python -m expansion_hunter.cli app --host 127.0.0.1 --port 8000
