#!/usr/bin/env bash
# @exports: verify.sh [--quick (default) | --full | --kit-only | --run-only] [--max-replay N=3] -> prints "PASS <id> <measured>" | "FAIL <id>: <reason>" | "SKIP <id> <reason>"; exit 0 iff no FAIL (2 on usage/preflight error)
# @imports: factory/selftest/test_lint.py; factory/lib/htmlcheck.py:check_page; factory/lib/sandbox.py:load_spec,build,start,wait_healthy,probe_no_egress,stop
# @imports: factory/bin/gate.py (--stage/--stage-dir, --commit, --seed, --env, --cache, --evidence-root); factory/bin/ratchet.py (add, fixed, verify-history); factory/bin/mandate_lint.py; factory/bin/trace.py; factory/bin/costreport.py
# @env: none
# @schema: checks: P1 python3.12 | RES committed results cite existing commits | K1 lint fixtures | K2 htmlcheck | K3 hammer racy>=9/10 safe 0/10 | K4 safe gate green | K5 racy gate red + evidence | K6 egress probe | K7 ratchet in throwaway repo
# @schema: checks: R1 per-stage gate | R2 inherited acceptance + ratchet | R3 replay red->green | R4 trace coverage | R5 no human-authored stage commits | R6 mandate lint | R7 cost.json matches evidence | R8 FACTORY.md block matches | R9 rework budget -> revert
# @schema: modes: quick = K1,K2,RES,R4-R9,R6, K3/R1/R2 cited from results/, R3 with 1 replay (SKIP w/o Docker); full = everything from scratch; kit-only = K1-K7; run-only = R1-R9 from scratch
# @schema: writes results/verify.json {"mode","commit","duration_s","ok","checks":[{"id","status","measured"}]}; also results/hammer.json (K3), results/stages.json (R1/R2), results/ratchet.json (R3), results/mandate_lint.json (R6), results/trace.json (R4/R5)
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "verify: cannot cd to $ROOT" >&2; exit 2; }

MODE=quick; DO_KIT=1; DO_RUN=1; KIT_DEEP=0; RUN_DEEP=0; MAXR=3

usage() {
  echo "usage: scripts/verify.sh [--quick|--full|--kit-only|--run-only] [--max-replay N]"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --quick)    MODE=quick;    DO_KIT=1; DO_RUN=1; KIT_DEEP=0; RUN_DEEP=0 ;;
    --full)     MODE=full;     DO_KIT=1; DO_RUN=1; KIT_DEEP=1; RUN_DEEP=1 ;;
    --kit-only) MODE=kit-only; DO_KIT=1; DO_RUN=0; KIT_DEEP=1; RUN_DEEP=0 ;;
    --run-only) MODE=run-only; DO_KIT=0; DO_RUN=1; KIT_DEEP=0; RUN_DEEP=1 ;;
    --max-replay) shift; MAXR="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "verify: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
case "$MAXR" in
  ''|*[!0-9]*) echo "verify: --max-replay needs a positive integer" >&2; exit 2 ;;
esac
if [ "$MAXR" -lt 1 ]; then echo "verify: --max-replay must be >= 1" >&2; exit 2; fi

if ! HEAD_SHA="$(git rev-parse HEAD 2>&1)"; then
  echo "FAIL P0: not a git repo with at least one commit ($HEAD_SHA). Run from the repo root after committing." >&2
  exit 2
fi

TMP="$(mktemp -d)" || { echo "verify: cannot create temp dir" >&2; exit 2; }
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
CHECKS="$TMP/checks.tsv"
: >"$CHECKS"
FAILS=0
START=$SECONDS
mkdir -p results

oneline() { tr '\n\t' '  ' | sed 's/  */ /g' | cut -c1-320; }

report() { # id status measured
  if [ "$2" = "FAIL" ]; then
    printf 'FAIL %s: %s\n' "$1" "$3"
    FAILS=$((FAILS + 1))
  else
    printf '%s %s %s\n' "$2" "$1" "$3"
  fi
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$CHECKS"
}

jget() { # file key -> value (length for lists/dicts)
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
v=d[sys.argv[2]]
print(len(v) if isinstance(v,(list,dict)) else v)' "$1" "$2"
}

DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then DOCKER=1; fi

emit_lines() { # reads "ID STATUS message" lines on stdin
  local id st msg
  while read -r id st msg; do
    [ -n "$id" ] && report "$id" "$st" "$msg"
  done
}

# ---------------------------------------------------------------- preflight
check_p1() {
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    report P1 PASS "$(python3 --version 2>&1)"
  else
    report P1 FAIL "python3 >= 3.12 required, found: $(python3 --version 2>&1)"
  fi
}

# ---------------------------------------------------------------- committed results
check_results() {
  local out rc
  if ! ls results/*.json >/dev/null 2>&1; then
    report RES FAIL "no results/*.json present; run the factory, then scripts/verify.sh --full"
    return
  fi
  out="$(python3 - <<'PY' 2>&1
import glob, json, subprocess, sys
bad, n = [], 0
for p in sorted(glob.glob("results/*.json")):
    if p.endswith("verify.json"):
        continue
    n += 1
    d = json.load(open(p))
    c = d.get("commit")
    if not isinstance(c, str) or not c:
        bad.append(p + ": no 'commit' field")
        continue
    r = subprocess.run(["git", "cat-file", "-e", c + "^{commit}"], capture_output=True)
    if r.returncode != 0:
        bad.append(p + ": commit " + c[:7] + " not in git history")
if n == 0:
    bad.append("only verify.json present")
if bad:
    print("; ".join(bad))
    sys.exit(1)
print("%d result files cite existing commits" % n)
PY
)"; rc=$?
  if [ $rc -eq 0 ]; then report RES PASS "$out"; else report RES FAIL "$(printf '%s' "$out" | oneline)"; fi
}

# ---------------------------------------------------------------- kit checks
check_k1() {
  local out rc
  out="$(python3 factory/selftest/test_lint.py 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    report K1 PASS "$(printf '%s\n' "$out" | grep -E '^Ran ' | head -1) (leaky fixture fails, clean passes)"
  else
    report K1 FAIL "test_lint.py exit $rc: $(printf '%s' "$out" | tail -4 | oneline)"
  fi
}

check_k2() {
  local out rc
  out="$(python3 - <<'PY' 2>&1
import sys
sys.path.insert(0, ".")
from factory.lib.htmlcheck import check_page
good = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>T</title>'
        '<style>.card{max-width:480px;width:100%}</style></head><body><main>'
        '<label for="a">A</label><input id="a" name="a"><label>B <input name="b"></label>'
        '<input type="hidden" name="h"></main></body></html>')
bad = '<html><head></head><body><div style="width: 900px"><input name="x"></div></body></html>'
want = sorted(["viewport-meta", "title", "input-labels", "no-fixed-width", "html-lang", "landmark"])
gf = [n for n, ok, _ in check_page(good) if not ok]
bf = sorted(n for n, ok, _ in check_page(bad) if not ok)
if gf:
    print("good page wrongly failed: %s" % gf)
    sys.exit(1)
if bf != want:
    print("bad page should fail all six checks, failed: %s" % bf)
    sys.exit(1)
print("good page 6/6 pass, bad page 6/6 caught")
PY
)"; rc=$?
  if [ $rc -eq 0 ]; then report K2 PASS "$out"; else report K2 FAIL "$(printf '%s' "$out" | oneline)"; fi
}

gate_selftest() { # mode seed item -> gate exit code
  python3 factory/bin/gate.py --stage-dir factory/selftest/service --item "$3" --seat seat-verify \
    --kind gate --seed "$2" --acceptance factory/selftest/acceptance --no-inherit --no-ratchet --cache \
    --evidence-root "$TMP/ev-kit" --env "MODE=$1" >"$TMP/$3.log" 2>&1
  return $?
}

check_k3_cited() {
  if [ ! -f results/hammer.json ]; then
    report K3 FAIL "results/hammer.json missing; run scripts/verify.sh --kit-only once and commit results/"
    return
  fi
  python3 - <<'PY' 2>&1 | emit_lines
import json, subprocess
d = json.load(open("results/hammer.json"))
c = d["commit"]
exists = subprocess.run(["git", "cat-file", "-e", c + "^{commit}"], capture_output=True).returncode == 0
hits, runs, fp, sruns = d["racy_detected"], d["racy_runs"], d["safe_false_positives"], d["safe_runs"]
msg = "racy caught %d/%d, safe false positives %d/%d @%s (cited from results/hammer.json)" % (hits, runs, fp, sruns, c[:7])
if exists and hits >= 9 and fp == 0:
    print("K3 PASS " + msg)
else:
    print("K3 FAIL " + msg + (" (commit missing from history)" if not exists else " (need racy>=9 and safe false positives=0)"))
PY
}

check_kit_deep() {
  local s hits=0 fp=0 rc infra=0 infra_log="" racy1="" safe1="" ev out
  if [ "$DOCKER" -ne 1 ]; then
    for id in K3 K4 K5 K6 K7; do report "$id" FAIL "Docker unavailable; start Docker and re-run"; done
    return
  fi
  for s in 1 2 3 4 5 6 7 8 9 10; do
    gate_selftest racy "$s" "kit-racy-s$s"; rc=$?
    [ "$s" -eq 1 ] && racy1=$rc
    case $rc in 1) hits=$((hits + 1)) ;; 2) infra=1; infra_log="$TMP/kit-racy-s$s.log" ;; esac
    gate_selftest safe "$s" "kit-safe-s$s"; rc=$?
    [ "$s" -eq 1 ] && safe1=$rc
    case $rc in 1) fp=$((fp + 1)) ;; 2) infra=1; infra_log="$TMP/kit-safe-s$s.log" ;; esac
  done
  if [ $infra -eq 1 ]; then
    report K3 FAIL "gate infra error (exit 2) during seed sweep; log tail: $(tail -3 "$infra_log" | oneline)"
    report K4 FAIL "gate infra error; fix Docker/sandbox first"
    report K5 FAIL "gate infra error; fix Docker/sandbox first"
  else
    python3 - "$hits" "$fp" "$HEAD_SHA" <<'PY'
import json, sys
hits, fp, commit = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
with open("results/hammer.json", "w", encoding="utf-8") as f:
    json.dump({"commit": commit, "racy_detected": hits, "racy_runs": 10, "safe_false_positives": fp,
               "safe_runs": 10, "seeds": list(range(1, 11))}, f, indent=2, sort_keys=True)
    f.write("\n")
PY
    if [ "$hits" -ge 9 ] && [ "$fp" -eq 0 ]; then
      report K3 PASS "racy caught $hits/10 seeds, safe false positives $fp/10"
    else
      report K3 FAIL "racy caught $hits/10 (need >=9), safe false positives $fp/10 (need 0)"
    fi
    if [ "$safe1" = "0" ]; then report K4 PASS "gate exit 0 on safe service"; else report K4 FAIL "gate exit $safe1 on safe service; see gate output of kit-safe-s1"; fi
    ev="$(find "$TMP/ev-kit" -path '*kit-racy-s1*' -name '*-gate.json' 2>/dev/null | sort | head -1)"
    if [ "$racy1" != "1" ]; then
      report K5 FAIL "gate exit $racy1 on racy service (expected 1)"
    elif [ -z "$ev" ]; then
      report K5 FAIL "gate failed the racy service but wrote no evidence JSON under evidence-root"
    else
      out="$(python3 - "$ev" <<'PY' 2>&1
import json, sys
d = json.load(open(sys.argv[1]))
bad = [c["name"] for c in d["checks"] if not c["ok"]]
if d["verdict"] != "fail" or not bad:
    print("verdict=%s failed_checks=%s" % (d["verdict"], bad))
    sys.exit(1)
print("verdict=fail failed_checks=%d" % len(bad))
PY
)"; rc=$?
      if [ $rc -eq 0 ]; then report K5 PASS "$out"; else report K5 FAIL "$(printf '%s' "$out" | oneline)"; fi
    fi
  fi

  out="$(python3 - <<'PY' 2>&1
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from factory.lib import sandbox
d = Path("factory/selftest/service")
spec = sandbox.load_spec(d)
tag = "ratchet-k6-egress"
name = "ratchet-k6-egress-%d" % os.getpid()
r = sandbox.build(d, spec, tag, True)
if not r.ok:
    print("build failed: " + r.stdout_tail)
    sys.exit(1)
try:
    r = sandbox.start(tag, name, spec)
    if not r.ok:
        print("start failed: " + r.stdout_tail)
        sys.exit(1)
    r = sandbox.wait_healthy(name, spec)
    if not r.ok:
        print("service not healthy: " + r.stdout_tail)
        sys.exit(1)
    r = sandbox.probe_no_egress(name)
    print(("TCP 1.1.1.1:443 and DNS both blocked" if r.ok else "EGRESS POSSIBLE: " + r.stdout_tail[-200:]))
    sys.exit(0 if r.ok else 1)
finally:
    sandbox.stop(name)
PY
)"; rc=$?
  if [ $rc -eq 0 ]; then report K6 PASS "$out"; else report K6 FAIL "$(printf '%s' "$out" | oneline)"; fi

  check_k7
}

check_k7() {
  local K7="$TMP/k7" A B out rc
  mkdir -p "$K7/stage-1" "$K7/ratchet/cx"
  cp -a "$ROOT/factory" "$K7/factory" && find "$K7/factory" -name __pycache__ -type d -prune -exec rm -rf {} +
  cp "$ROOT/factory/selftest/service/app.py" "$ROOT/factory/selftest/service/Dockerfile" \
     "$ROOT/factory/selftest/service/service.json" "$K7/stage-1/" || { report K7 FAIL "cannot copy selftest service"; return; }
  cp "$ROOT/seats.json" "$K7/seats.json" 2>/dev/null
  git -C "$K7" init -q || { report K7 FAIL "git init failed in throwaway repo"; return; }
  git -C "$K7" config user.name seat-verify
  git -C "$K7" config user.email seat-verify@factory.local
  set_mode() {
    python3 - "$K7/stage-1/service.json" "$1" <<'PY'
import json, sys
p, mode = sys.argv[1], sys.argv[2]
d = json.load(open(p))
d.setdefault("env", {})["MODE"] = mode
with open(p, "w") as f:
    json.dump(d, f, indent=2, sort_keys=True)
    f.write("\n")
PY
  }
  set_mode racy || { report K7 FAIL "cannot set MODE=racy in throwaway service.json"; return; }
  git -C "$K7" add -A && git -C "$K7" commit -q -m "k7: racy stage" || { report K7 FAIL "throwaway commit A failed"; return; }
  A="$(git -C "$K7" rev-parse HEAD)"
  cat >"$K7/ratchet/cx/CX-0001.py" <<'PYRUN'
import json, os, sys, threading, urllib.error, urllib.request
base = os.environ["BASE_URL"]
cap = json.load(urllib.request.urlopen(base + "/state", timeout=10))["capacity"]
N = 50
barrier = threading.Barrier(N)
lock = threading.Lock()
codes = []
def worker():
    barrier.wait()
    req = urllib.request.Request(base + "/claim", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        code = urllib.request.urlopen(req, timeout=15).status
    except urllib.error.HTTPError as e:
        code = e.code
    with lock:
        codes.append(code)
threads = [threading.Thread(target=worker) for _ in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
if len(codes) != N:
    print(json.dumps({"violated": False, "detail": "only %d/%d requests completed" % (len(codes), N)}))
    sys.exit(2)
claimed = codes.count(200)
violated = claimed > cap
print(json.dumps({"violated": violated, "detail": "claimed %d of capacity %d" % (claimed, cap)}))
sys.exit(1 if violated else 0)
PYRUN
  out="$(python3 "$K7/factory/bin/ratchet.py" add --id CX-0001 --stage 1 --found-at "$A" --title "over-claim under concurrency" \
      --invariant "claims never exceed capacity" --runner ratchet/cx/CX-0001.py --seed 1 --seat seat-verify 2>&1)" \
    || { report K7 FAIL "ratchet add failed: $(printf '%s' "$out" | oneline)"; return; }
  set_mode safe || { report K7 FAIL "cannot set MODE=safe in throwaway service.json"; return; }
  git -C "$K7" add -A && git -C "$K7" commit -q -m "k7: fix" || { report K7 FAIL "throwaway commit B failed"; return; }
  B="$(git -C "$K7" rev-parse HEAD)"
  out="$(python3 "$K7/factory/bin/ratchet.py" fixed --id CX-0001 --fixed-at "$B" 2>&1)" \
    || { report K7 FAIL "ratchet fixed failed: $(printf '%s' "$out" | oneline)"; return; }
  git -C "$K7" add -A && git -C "$K7" commit -q -m "k7: record counterexample" || { report K7 FAIL "throwaway commit C failed"; return; }
  out="$(python3 "$K7/factory/bin/ratchet.py" verify-history --max 1 --out results/ratchet.json --cache 2>&1)"; rc=$?
  if [ $rc -ne 0 ]; then report K7 FAIL "verify-history exit $rc: $(printf '%s' "$out" | tail -4 | oneline)"; return; fi
  out="$(python3 - "$K7/results/ratchet.json" <<'PY' 2>&1
import json, sys
c = json.load(open(sys.argv[1]))["checked"][0]
if not (c["at_found"] == "VIOLATED" and c["at_fixed"] == "HELD"):
    print("at_found=%s at_fixed=%s" % (c["at_found"], c["at_fixed"]))
    sys.exit(1)
print("throwaway repo: VIOLATED@%s HELD@%s" % (c["found_at"][:7], c["fixed_at"][:7]))
PY
)"; rc=$?
  if [ $rc -eq 0 ]; then report K7 PASS "$out"; else report K7 FAIL "$(printf '%s' "$out" | oneline)"; fi
}

# ---------------------------------------------------------------- run checks
check_r6() {
  local code=() d out rc
  if [ ! -f tasks/_other_track.md ] || ! ls tasks/stage-*.md >/dev/null 2>&1; then
    report R6 FAIL "tasks/stage-N.md and tasks/_other_track.md must exist (paste the task text verbatim)"
    return
  fi
  for d in stage-*; do [ -d "$d" ] && code+=("$d"); done
  if [ "${#code[@]}" -gt 0 ]; then
    out="$(python3 factory/bin/mandate_lint.py --task tasks/stage-*.md tasks/_other_track.md --code "${code[@]}" --out results/mandate_lint.json 2>&1)"; rc=$?
  else
    out="$(python3 factory/bin/mandate_lint.py --task tasks/stage-*.md tasks/_other_track.md --out results/mandate_lint.json 2>&1)"; rc=$?
  fi
  if [ $rc -eq 0 ]; then
    report R6 PASS "$(jget results/mandate_lint.json tokens_checked) tokens, $(jget results/mandate_lint.json leaks) leaks, $(jget results/mandate_lint.json code_overlaps) code overlaps (warn)"
  else
    report R6 FAIL "lint exit $rc: $(printf '%s' "$out" | tail -3 | oneline)"
  fi
}

check_trace() {
  local stages out rc
  if ! git rev-parse -q --verify refs/tags/factory-start >/dev/null; then
    report R4 FAIL "tag factory-start missing; tag the frozen kit before the run"
    report R5 FAIL "tag factory-start missing; tag the frozen kit before the run"
    return
  fi
  if [ ! -e room-export ]; then
    report R4 FAIL "room-export/ missing; export the room after each stage"
    report R5 FAIL "room-export/ missing; export the room after each stage"
    return
  fi
  stages="$(ls -d stage-* 2>/dev/null | sed 's/^stage-//' | grep -E '^[0-9]+$' | sort -n | tr '\n' ' ')"
  if [ -z "$stages" ]; then
    report R4 FAIL "no stage-N/ directories"; report R5 FAIL "no stage-N/ directories"; return
  fi
  # shellcheck disable=SC2086
  out="$(python3 factory/bin/trace.py --since factory-start --stages $stages --room-export room-export --out results/trace.json 2>&1)"; rc=$?
  if [ $rc -eq 2 ]; then
    report R4 FAIL "trace infra error: $(printf '%s' "$out" | tail -2 | oneline)"
    report R5 FAIL "trace infra error: $(printf '%s' "$out" | tail -2 | oneline)"
    return
  fi
  python3 - <<'PY' 2>&1 | emit_lines
import json
d = json.load(open("results/trace.json"))
n, t, h = d["commits"], d["traceable"], len(d["human_authored"])
cov = d["coverage"]
print("R4 %s %d/%d stage commits traceable (%.0f%%)" % ("PASS" if n > 0 and cov >= d["min_coverage"] else "FAIL", t, n, cov * 100))
print("R5 %s %d human-authored of %d stage commits" % ("PASS" if n > 0 and h == 0 else "FAIL", h, n))
PY
}

check_r7_r8_r9() {
  local out rc
  if [ ! -f results/cost.json ]; then
    report R7 FAIL "results/cost.json missing; run: python3 factory/bin/costreport.py --out results/cost.json --inject FACTORY.md"
  elif out="$(python3 factory/bin/costreport.py --out "$TMP/cost.json" 2>&1)"; then
    out="$(python3 - results/cost.json "$TMP/cost.json" <<'PY' 2>&1
import json, sys
a, b = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
if a["stages"] != b["stages"]:
    print("results/cost.json differs from numbers recomputed from evidence/")
    sys.exit(1)
print("%d stages match evidence/" % len(a["stages"]))
PY
)"; rc=$?
    if [ $rc -eq 0 ]; then report R7 PASS "$out"; else report R7 FAIL "$out"; fi
  else
    report R7 FAIL "costreport failed: $(printf '%s' "$out" | tail -2 | oneline)"
  fi

  cp FACTORY.md "$TMP/FACTORY.md" 2>/dev/null || { report R8 FAIL "FACTORY.md missing"; check_r9; return; }
  if out="$(python3 factory/bin/costreport.py --out "$TMP/cost2.json" --inject "$TMP/FACTORY.md" 2>&1)"; then
    if diff -q FACTORY.md "$TMP/FACTORY.md" >/dev/null; then
      report R8 PASS "FACTORY.md cost block equals numbers from evidence/"
    else
      report R8 FAIL "FACTORY.md cost block is stale; run: python3 factory/bin/costreport.py --out results/cost.json --inject FACTORY.md"
    fi
  else
    report R8 FAIL "costreport inject failed: $(printf '%s' "$out" | tail -2 | oneline)"
  fi
  check_r9
}

check_r9() {
  local out
  if out="$(python3 factory/bin/costreport.py --out "$TMP/cost3.json" --assert-budget 2>&1)"; then
    report R9 PASS "$(printf '%s' "$out" | grep '^PASS' | sed 's/^PASS budget: //' | oneline)"
  else
    report R9 FAIL "$(printf '%s' "$out" | grep -E '^(FAIL|costreport: ERROR)' | oneline)"
  fi
}

check_stages_cited() {
  if [ ! -f results/stages.json ]; then
    report R1 FAIL "results/stages.json missing; run scripts/verify.sh --run-only on the finished run and commit results/"
    report R2 FAIL "results/stages.json missing; run scripts/verify.sh --run-only on the finished run and commit results/"
    return
  fi
  python3 - <<'PY' 2>&1 | emit_lines
import json, subprocess
d = json.load(open("results/stages.json"))
st = d["stages"]
def exists(c):
    return subprocess.run(["git", "cat-file", "-e", c + "^{commit}"], capture_output=True).returncode == 0
for rid, key in (("R1", "r1_exit"), ("R2", "r2_exit")):
    bad = [str(s["stage"]) for s in st if s[key] != 0 or not exists(s["commit"])]
    if st and not bad:
        print("%s PASS %d stages green at tagged commits (cited from results/stages.json)" % (rid, len(st)))
    else:
        print("%s FAIL stages_not_green_or_commit_missing=%s" % (rid, ",".join(bad) or "no_stages"))
PY
}

check_stages_deep() {
  local nums n sha r1 r2 b1="" b2="" cnt=0
  if [ "$DOCKER" -ne 1 ]; then
    report R1 FAIL "Docker unavailable; start Docker and re-run"
    report R2 FAIL "Docker unavailable; start Docker and re-run"
    return
  fi
  : >"$TMP/stages.tsv"
  nums="$(git tag -l 'green/stage-*' | sed 's#^green/stage-##' | grep -E '^[0-9]+$' | sort -n)"
  if [ -z "$nums" ]; then
    report R1 FAIL "no green/stage-N tags; stages are promoted with factory/bin/stage_advance.sh"
    report R2 FAIL "no green/stage-N tags; stages are promoted with factory/bin/stage_advance.sh"
    return
  fi
  for n in $nums; do
    sha="$(git rev-parse "green/stage-$n^{commit}")"
    python3 factory/bin/gate.py --stage "$n" --item "verify-r1-s$n" --seat seat-verify --kind gate \
      --commit "green/stage-$n" --no-inherit --no-ratchet --cache --evidence-root "$TMP/ev-run" >"$TMP/r1-$n.log" 2>&1; r1=$?
    python3 factory/bin/gate.py --stage "$n" --item "verify-r2-s$n" --seat seat-verify --kind gate \
      --commit "green/stage-$n" --cache --evidence-root "$TMP/ev-run" >"$TMP/r2-$n.log" 2>&1; r2=$?
    printf '%s\t%s\t%s\t%s\n' "$n" "$sha" "$r1" "$r2" >>"$TMP/stages.tsv"
    [ "$r1" -ne 0 ] && b1="$b1 $n(exit $r1)"
    [ "$r2" -ne 0 ] && b2="$b2 $n(exit $r2)"
    cnt=$((cnt + 1))
  done
  python3 - "$TMP/stages.tsv" "$HEAD_SHA" <<'PY'
import json, sys
rows = [ln.rstrip("\n").split("\t") for ln in open(sys.argv[1]) if ln.strip()]
out = {"commit": sys.argv[2], "stages": [
    {"stage": int(n), "tag": "green/stage-%s" % n, "commit": sha, "r1_exit": int(a), "r2_exit": int(b)}
    for n, sha, a, b in rows]}
with open("results/stages.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, sort_keys=True)
    f.write("\n")
PY
  if [ -z "$b1" ]; then report R1 PASS "$cnt stages build, serve, pass acceptance with no egress"; else report R1 FAIL "stages failing:$b1 (logs are in the gate output; re-run gate.py --stage N --commit green/stage-N)"; fi
  if [ -z "$b2" ]; then report R2 PASS "$cnt stages pass inherited acceptance + ratchet"; else report R2 FAIL "stages failing:$b2"; fi
}

check_r3() {
  local n out rc
  if [ "$DOCKER" -ne 1 ]; then
    if [ "$RUN_DEEP" -eq 1 ]; then report R3 FAIL "Docker unavailable; start Docker and re-run"; else report R3 SKIP "Docker absent; replay needs 2 builds"; fi
    return
  fi
  if [ "$RUN_DEEP" -eq 1 ]; then n="$MAXR"; else n=1; fi
  out="$(python3 factory/bin/ratchet.py verify-history --max "$n" --out results/ratchet.json --cache 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    report R3 PASS "$(jget results/ratchet.json checked) counterexample(s) VIOLATED@found_at and HELD@fixed_at"
  else
    report R3 FAIL "verify-history exit $rc: $(printf '%s' "$out" | tail -4 | oneline)"
  fi
}

# ---------------------------------------------------------------- run
echo "verify: mode=$MODE docker=$DOCKER head=${HEAD_SHA:0:7}"
check_p1

if [ "$DO_KIT" -eq 1 ]; then
  check_k1
  check_k2
  if [ "$KIT_DEEP" -eq 1 ]; then
    check_kit_deep
  else
    check_k3_cited
    for id in K4 K5 K6 K7; do report "$id" SKIP "deep kit check; run scripts/verify.sh --kit-only"; done
  fi
fi

if [ "$DO_RUN" -eq 1 ]; then
  check_results
  check_r6
  check_trace
  check_r7_r8_r9
  if [ "$RUN_DEEP" -eq 1 ]; then check_stages_deep; else check_stages_cited; fi
  check_r3
fi

DURATION=$((SECONDS - START))
python3 - "$CHECKS" "$MODE" "$HEAD_SHA" "$DURATION" <<'PY'
import json, sys
rows = [ln.rstrip("\n").split("\t", 2) for ln in open(sys.argv[1]) if ln.strip()]
checks = [{"id": i, "status": s, "measured": m} for i, s, m in rows]
out = {"mode": sys.argv[2], "commit": sys.argv[3], "duration_s": int(sys.argv[4]),
       "ok": not any(c["status"] == "FAIL" for c in checks), "checks": checks}
with open("results/verify.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, sort_keys=True)
    f.write("\n")
PY
PASSES="$(awk -F'\t' '$2=="PASS"' "$CHECKS" | wc -l | tr -d ' ')"
SKIPS="$(awk -F'\t' '$2=="SKIP"' "$CHECKS" | wc -l | tr -d ' ')"
echo "verify: $PASSES PASS, $FAILS FAIL, $SKIPS SKIP in ${DURATION}s (mode=$MODE) -> results/verify.json"
if [ "$FAILS" -gt 0 ]; then exit 1; fi
exit 0
