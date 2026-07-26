#!/bin/bash
# Short wrapper so the health check is a single typed word, not a pasted line.
# A mangled paste is indistinguishable from a broken command, and produced a
# confusing "nothing happens" once already.
cd "$(dirname "$0")" || exit 1
set -a; [ -f .env ] && . ./.env; set +a
exec .venv/bin/python -m src.main doctor "$@"
