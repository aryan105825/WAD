#!/usr/bin/env bash
# Launcher for BAND seat "seat-steward". Command: this file; Arguments: acp
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if command -v opencode >/dev/null 2>&1; then
  OC="$(command -v opencode)"
elif [ -x "$HOME/.npm-global/bin/opencode" ]; then
  OC="$HOME/.npm-global/bin/opencode"
else
  echo "seat-steward: opencode not found; install it (npm install -g opencode-ai)" >&2
  exit 127
fi
export GIT_AUTHOR_NAME="seat-steward" GIT_AUTHOR_EMAIL="seat-steward@factory.local"
export GIT_COMMITTER_NAME="seat-steward" GIT_COMMITTER_EMAIL="seat-steward@factory.local"
export OPENCODE_CONFIG="$ROOT/seats/seat-steward.json"
exec "$OC" "$@"
