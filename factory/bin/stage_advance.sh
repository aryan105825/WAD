#!/usr/bin/env bash
# @exports: stage_advance.sh N -> exit 0 promoted | 1 not promotable or precondition failed | 2 usage
# @imports: factory/bin/gate.py:--promotable N (exit 0 iff promotable)
# @env: none (git identity is set per-command for seat-steward)
# @schema: CLI: bash factory/bin/stage_advance.sh N
# @schema: effects: tag green/stage-N at HEAD; copy stage-N/ to stage-(N+1)/ if missing and commit as seat-steward
# @schema: commit trailers: Item: stage-N-promotion | Seat: seat-steward | Room-Ref: -
set -euo pipefail

if [ "$#" -ne 1 ] || ! [[ "$1" =~ ^[0-9]+$ ]]; then
  echo "usage: stage_advance.sh N   (N = stage number to promote)" >&2
  exit 2
fi
N="$1"
NEXT=$((N + 1))
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ ! -d "stage-$N" ]; then
  echo "stage_advance: stage-$N/ does not exist in $ROOT. Run from a repo that contains the stage directory." >&2
  exit 1
fi

if ! python3 factory/bin/gate.py --promotable "$N"; then
  echo "stage_advance: stage $N is not promotable. It needs a passing gate AND adversarial evidence for the same commit." >&2
  exit 1
fi

TAG="green/stage-$N"
HEAD_SHA="$(git rev-parse HEAD)"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  TAG_SHA="$(git rev-parse "$TAG^{commit}")"
  if [ "$TAG_SHA" != "$HEAD_SHA" ]; then
    echo "stage_advance: tag $TAG already points at ${TAG_SHA:0:7}, not HEAD ${HEAD_SHA:0:7}. Tags are permanent; resolve by hand." >&2
    exit 1
  fi
  echo "stage_advance: $TAG already at HEAD, continuing"
else
  git tag "$TAG" "$HEAD_SHA"
  echo "TAGGED $TAG ${HEAD_SHA:0:7}"
fi

if [ -d "stage-$NEXT" ]; then
  echo "stage_advance: stage-$NEXT/ already exists, nothing to seed"
else
  cp -a "stage-$N" "stage-$NEXT"
  find "stage-$NEXT" -name __pycache__ -type d -prune -exec rm -rf {} +
  git add "stage-$NEXT"
  export GIT_AUTHOR_NAME="seat-steward" GIT_AUTHOR_EMAIL="seat-steward@factory.local"
  export GIT_COMMITTER_NAME="seat-steward" GIT_COMMITTER_EMAIL="seat-steward@factory.local"
  printf 'steward: promote stage %s, seed stage %s\n\nItem: stage-%s-promotion\nSeat: seat-steward\nRoom-Ref: -\n' \
    "$N" "$NEXT" "$N" | git commit -q -F -
  echo "SEEDED stage-$NEXT from stage-$N"
fi
echo "PROMOTED stage-$N"
