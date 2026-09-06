#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
ENV_FILE="${MEDIACENTER_ENV_FILE:-/srv/mediacenter/config/mediacenter.env}"
test -f "$ENV_FILE"
set -a
. "$ENV_FILE"
set +a
exec python3 -m mediacenter.server
