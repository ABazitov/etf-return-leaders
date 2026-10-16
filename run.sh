#!/bin/bash
# Start ETF Return Leaders. First run: installs everything and asks three setup questions.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Python environment (first run only)…"
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "This app needs Python 3.11 or newer. Install it, delete the .venv folder, and run again."
  exit 1
fi
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m playwright install chromium >/dev/null
if [ ! -f config.toml ]; then
  .venv/bin/python -m app.setup config.toml
fi
exec .venv/bin/python -m app.main
