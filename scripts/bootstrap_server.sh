#!/usr/bin/env bash
set -euo pipefail

SCRIPT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
exec python3 "$SCRIPT_ROOT/install_server_release.py" "$@"
