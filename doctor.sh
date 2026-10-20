#!/usr/bin/env bash
# Run the aidir doctor from the project's virtual environment.
# Every argument is forwarded after the `doctor` command: ./doctor.sh --help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "doctor.sh: $PYTHON is missing; run ./install.sh first" >&2
  exit 2
fi

exec "$PYTHON" "$ROOT/core/app.py" doctor "$@"
