#!/usr/bin/env bash
# The project's quality gate: every check that must pass before a change lands.
#
# Usage:
#   scripts/check.sh            # run every gate
#   scripts/check.sh --fix      # fix what can be fixed (format, import order)
#
# The script fails on the first gate that fails, so a broken gate is never
# hidden behind a later one.  It runs the offline test suite only: the live
# checks are opt-in with PYZILL_LIVE=1 and are never part of this gate.

set -euo pipefail

cd "$(dirname "$0")/.."

# Use the repository virtual environment when there is one, so the gate runs
# the same tool versions the project pins.
if [[ -x .venv/bin/python ]]; then
    PYTHON=.venv/bin/python
    RUFF=.venv/bin/ruff
    MYPY=.venv/bin/mypy
    VULTURE=.venv/bin/vulture
else
    PYTHON=${PYTHON:-python3}
    RUFF=${RUFF:-ruff}
    MYPY=${MYPY:-mypy}
    VULTURE=${VULTURE:-vulture}
fi

FIX=false
if [[ "${1:-}" == "--fix" ]]; then
    FIX=true
fi

run_gate() {
    local name=$1
    shift
    echo
    echo "=== $name ==="
    "$@"
}

if [[ "$FIX" == true ]]; then
    run_gate "format (fix)" "$RUFF" format src tests scripts
    run_gate "lint (fix)" "$RUFF" check --fix src tests scripts
else
    run_gate "format" "$RUFF" format --check src tests scripts
    run_gate "lint" "$RUFF" check src tests scripts
fi

run_gate "type-check" "$MYPY" --strict
run_gate "dead code" "$VULTURE" src
run_gate "tests" "$PYTHON" -m pytest -q

echo
echo "All gates passed."
