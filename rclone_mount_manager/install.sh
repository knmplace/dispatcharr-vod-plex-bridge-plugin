#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f /etc/unraid-version ]]; then
  exec bash "$SCRIPT_DIR/install-unraid.sh"
fi

exec bash "$SCRIPT_DIR/install-linux.sh"
