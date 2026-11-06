#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Verwrity encrypted backup (Python 3.11 or newer required)…"
  python3 -m venv .venv || { read -r -p "Couldn't create the environment. Press Enter to close."; exit 1; }
fi
if ! .venv/bin/python -c 'import cryptography' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt || { read -r -p "Couldn't install dependencies. Check your internet connection. Press Enter to close."; exit 1; }
fi
.venv/bin/python backup.py "$@"
result=$?
read -r -p "Press Enter to close."
exit "$result"
