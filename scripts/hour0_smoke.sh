#!/usr/bin/env bash
# @exports: CLI: bash scripts/hour0_smoke.sh  (no flags)
# @imports: none (reads .env at repo root if present)
# @env: FEATHERLESS_API_KEY
# @env: FEATHERLESS_BASE_URL
# @env: FEATHERLESS_MODEL
# @env: FACTORY_DRIVER_IMAGE
# @schema: stdout lines "PASS|FAIL|MANUAL H<n> <detail>"; H1 docker+egress, H2 python, H3 Featherless, H4 agent drive (manual), H5 room export (manual unless fixture exists)
# @schema: exit 0 when no probe FAILs (MANUAL steps do not fail but are listed at the end); exit 1 otherwise
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

FEATHERLESS_API_KEY="${FEATHERLESS_API_KEY:-}"
FEATHERLESS_BASE_URL="${FEATHERLESS_BASE_URL:-https://api.featherless.ai/v1}"
FEATHERLESS_MODEL="${FEATHERLESS_MODEL:-}"
DRIVER_IMAGE="${FACTORY_DRIVER_IMAGE:-python:3.12.7-slim}"
export FEATHERLESS_API_KEY FEATHERLESS_BASE_URL FEATHERLESS_MODEL

FAILS=0
MANUALS=""

pass() { printf 'PASS %s %s\n' "$1" "$2"; }
fail() { printf 'FAIL %s %s\n' "$1" "$2"; FAILS=$((FAILS + 1)); }
manual() { printf 'MANUAL %s %s\n' "$1" "$2"; MANUALS="$MANUALS $1"; }

EGRESS_PY='import socket, sys
print("hello")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
except OSError:
    sys.exit(0)
print("EGRESS OPEN")
sys.exit(1)'

read -r -d '' H3_PY <<'PY'
import json, os, sys, urllib.error, urllib.request
base = os.environ["FEATHERLESS_BASE_URL"].rstrip("/")
payload = {
    "model": os.environ["FEATHERLESS_MODEL"],
    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    "max_tokens": 8,
}
request = urllib.request.Request(
    base + "/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["FEATHERLESS_API_KEY"]},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        status = response.status
        body = response.read()
except urllib.error.HTTPError as exc:
    print("HTTP %d: %s" % (exc.code, exc.read()[:200].decode("utf-8", "replace")))
    sys.exit(1)
except OSError as exc:
    print("network error: %s" % exc)
    sys.exit(1)
try:
    text = json.loads(body)["choices"][0]["message"]["content"]
except (ValueError, KeyError, IndexError, TypeError) as exc:
    print("HTTP %d but the body is not an OpenAI-style chat completion (%s)" % (status, exc))
    sys.exit(1)
print("HTTP %d model=%s reply=%r" % (status, os.environ["FEATHERLESS_MODEL"], str(text)[:40]))
PY

h1() {
  if ! command -v docker >/dev/null 2>&1; then
    fail H1 "docker CLI not found. Install Docker >= 24."
    return
  fi
  local version
  if ! version="$(docker version --format '{{.Server.Version}}' 2>&1)"; then
    fail H1 "docker daemon not reachable: $version. Start Docker and make sure your user may run it."
    return
  fi
  local major="${version%%.*}"
  case "$major" in
    '' | *[!0-9]*) fail H1 "cannot parse docker server version '$version'."; return ;;
  esac
  if [ "$major" -lt 24 ]; then
    fail H1 "docker server $version is older than 24. Upgrade Docker."
    return
  fi
  local out rc
  out="$(docker run --rm --network none "$DRIVER_IMAGE" python3 -c "$EGRESS_PY" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    pass H1 "docker $version ran $DRIVER_IMAGE; egress blocked under --network none"
  elif printf '%s' "$out" | grep -q "EGRESS OPEN"; then
    fail H1 "egress is NOT blocked under --network none. Check the Docker daemon configuration."
  else
    fail H1 "docker run failed (exit $rc): $(printf '%s' "$out" | tail -n 3 | tr '\n' ' '). Check that the image $DRIVER_IMAGE can be pulled."
  fi
}

h2() {
  if ! command -v python3 >/dev/null 2>&1; then
    fail H2 "python3 not found. Install Python 3.12."
    return
  fi
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    pass H2 "python $(python3 -c 'import platform; print(platform.python_version())')"
  else
    fail H2 "python3 is $(python3 -c 'import platform; print(platform.python_version())'); need >= 3.12."
  fi
}

h3() {
  if ! command -v python3 >/dev/null 2>&1; then
    fail H3 "python3 is needed to run this probe (see H2)."
    return
  fi
  if [ -z "$FEATHERLESS_API_KEY" ]; then
    fail H3 "FEATHERLESS_API_KEY is not set. Copy .env.example to .env and fill it in."
    return
  fi
  if [ -z "$FEATHERLESS_MODEL" ]; then
    fail H3 "FEATHERLESS_MODEL is not set. Put a model id from your Featherless account in .env."
    return
  fi
  local out
  if out="$(python3 -c "$H3_PY" 2>&1)"; then
    pass H3 "$out"
  else
    fail H3 "$out"
  fi
}

h4() {
  manual H4 "drive the Featherless model from a BAND-supported coding-agent runtime:"
  printf '       1. In BAND Desktop, create a seat whose coding-agent runtime points at %s with model %s.\n' "$FEATHERLESS_BASE_URL" "${FEATHERLESS_MODEL:-<unset>}"
  printf '       2. Dispatch a one-line task (create a file and commit it) and confirm the seat edits and commits.\n'
  printf '       3. If the runtime cannot use a custom OpenAI-compatible endpoint, set seat-breaker to a different native\n'
  printf '          model family in seats.json (provider "native") instead.\n'
}

h5() {
  if compgen -G "$ROOT/factory/selftest/fixtures/room_sample.*" >/dev/null; then
    local sample
    sample="$(compgen -G "$ROOT/factory/selftest/fixtures/room_sample.*" | head -n 1)"
    if [ -s "$sample" ]; then
      pass H5 "room export sample present: ${sample#"$ROOT"/}"
      return
    fi
    fail H5 "${sample#"$ROOT"/} is empty. Re-export the room and save it again."
    return
  fi
  manual H5 "export a short test room from BAND and save it as factory/selftest/fixtures/room_sample.<ext> (keep the native format)."
}

h1
h2
h3
h4
h5

if [ -n "$MANUALS" ]; then
  printf 'Manual steps outstanding:%s\n' "$MANUALS"
fi
if [ "$FAILS" -gt 0 ]; then
  printf 'RESULT FAIL (%d failing probe(s))\n' "$FAILS"
  exit 1
fi
printf 'RESULT OK (no failing probes)\n'
exit 0
