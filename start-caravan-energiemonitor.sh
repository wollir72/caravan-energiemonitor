#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$HOME/devel/caravan-energiemonitor"
VENV_DIR="$HOME/bin/venvs/victron"
APP="$VENV_DIR/bin/caravan-energiemonitor"

if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "Fehler: Projektverzeichnis nicht gefunden:"
    echo "  $PROJECT_DIR"
    exit 1
fi

if [[ ! -x "$APP" ]]; then
    echo "Fehler: Startprogramm nicht gefunden:"
    echo "  $APP"
    echo
    echo "Installiere das Projekt erneut mit:"
    echo "  source \"$VENV_DIR/bin/activate\""
    echo "  cd \"$PROJECT_DIR\""
    echo "  python -m pip install -e ."
    exit 1
fi

cd "$PROJECT_DIR"
exec "$APP" "$@"
