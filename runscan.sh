#!/bin/bash
# Activates the venv, loads .env, and runs a src.main CLI command. Lives at the
# project root and locates everything relative to itself, so it works on any
# host / user (OVH, Google Cloud, ...) without hard-coded paths.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

source .venv/bin/activate
set -a
[ -f .env ] && source .env
set +a

exec python -m src.main "$@"
