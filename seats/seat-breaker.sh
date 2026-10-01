#!/usr/bin/env bash
# Launcher for BAND seat "seat-breaker". Command: this file; Arguments: acp
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if command -v opencode >/dev/null 2>&1; then
  OC="$(command -v opencode)"
elif [ -x "$HOME/.npm-global/bin/opencode" ]; then
  OC="$HOME/.npm-global/bin/opencode"
else
  echo "seat-breaker: opencode not found; install it (npm install -g opencode-ai)" >&2
  exit 127
fi
export GIT_AUTHOR_NAME="seat-breaker" GIT_AUTHOR_EMAIL="seat-breaker@factory.local"
export GIT_COMMITTER_NAME="seat-breaker" GIT_COMMITTER_EMAIL="seat-breaker@factory.local"
export OPENCODE_CONFIG="$ROOT/seats/seat-breaker.json"
exec "$OC" "$@"
