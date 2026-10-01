# @exports: main(argv: list[str]|None=None) -> int  # exit 0 pass | 1 fail | 2 infra
# @imports: factory/lib/common.py:SCHEMA_VERSION, CheckResult, Evidence, git, load_config, now_iso, snapshot
# @imports: factory/lib/evidence.py:evidence_path, promotable, rework_cycle, write_evidence
# @imports: factory/lib/sandbox.py:Sandbox, load_spec
# @env: none (FACTORY_* are read by common.load_config)
# @schema: gate.py (--stage N | --stage-dir DIR) --item ID --seat NAME [--kind gate|adversarial] [--commit REF=HEAD] [--seed INT=1] [--repeat K=1] [--acceptance DIR] [--env K=V ...] [--no-inherit] [--no-ratchet] [--cache] [--evidence-root evidence]
# @schema: gate.py --promotable N [--commit REF=HEAD] [--evidence-root evidence]  # prints PROMOTABLE|NOT_PROMOTABLE, exit 0 iff promotable
# @schema: DIR and --acceptance are repo-relative paths inside the commit snapshot; --stage N means DIR=stage-N and acceptance=stage-N/acceptance
# @schema: --stage-dir named exactly stage-N records stage N and inherits stages 1..N-1; any other --stage-dir records stage 1 and does not inherit
# @schema: --env K=V overrides the service container env (repeat the flag or list several values after it)
# @schema: --repeat K runs every acceptance and inherited check K times with SEED=seed..seed+K-1; the ratchet check runs once
# @schema: waiver ids in service.json: "stage-K/check_x.py" skips that inherited check; "CX-NNNN" is passed to `ratchet.py check --waive CX-NNNN` (one flag per id)
# @schema: ratchet step runs `python3 factory/bin/ratchet.py check --base-url URL [--waive ID]...` inside the service namespace, only when the snapshot has ratchet/CX-*.json
# @schema: classification: build/healthy failures and check exit 1 = fail; start/egress-probe failures and check exits other than 0/1 = infra; any fail wins over infra
# @schema: stdout: "PASS|FAIL <check> (<s>s)" per check, last line "EVIDENCE <path>"; notes and failure output go to stderr
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from factory.lib.common import SCHEMA_VERSION, CheckResult, Evidence, git, load_config, now_iso, snapshot  # noqa: E402
from factory.lib.evidence import evidence_path, promotable, rework_cycle, write_evidence  # noqa: E402
from factory.lib.sandbox import Sandbox, load_spec  # noqa: E402

CHECK_TIMEOUT_S = 300
RATCHET_TIMEOUT_S = 600
_CX_RE = re.compile(r"^CX-\d{4}$")
_STAGE_RE = re.compile(r"^stage-(\d+)$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gate.py", description="Clean-room gate: build, run isolated, check, record evidence.")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--stage", type=int, help="stage number N (stage directory stage-N)")
    target.add_argument("--stage-dir", help="repo-relative stage directory")
    parser.add_argument("--item", help="item id the evidence is recorded under")
    parser.add_argument("--seat", help="seat name recorded in the evidence file name")
    parser.add_argument("--kind", choices=("gate", "adversarial"), default="gate")
    parser.add_argument("--commit", default="HEAD", help="git ref to snapshot (default HEAD)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--acceptance", help="repo-relative directory holding check_*.py (default <stage dir>/acceptance)")
    parser.add_argument("--env", nargs="+", action="extend", default=[], metavar="K=V", help="service container env overrides")
    parser.add_argument("--no-inherit", action="store_true")
    parser.add_argument("--no-ratchet", action="store_true")
    parser.add_argument("--cache", action="store_true", help="allow the docker build cache")
    parser.add_argument("--evidence-root", default="evidence")
    parser.add_argument("--promotable", type=int, metavar="N", help="exit 0 iff stage N is promotable at --commit")
    return parser


def _parse_env(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--env value {item!r} must look like KEY=VALUE.")
        result[key] = value
    return result


def _rel_path(value: str, flag: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{flag} {value!r} must be a repo-relative path without '..'.")
    text = path.as_posix().strip("/")
    if not text or text == ".":
        raise ValueError(f"{flag} {value!r} must name a directory inside the repo.")
    return text


def _resolve_stage(args: argparse.Namespace) -> tuple[int, str]:
    if args.stage is not None:
        if args.stage < 1:
            raise ValueError(f"--stage must be >= 1, got {args.stage}.")
        return args.stage, f"stage-{args.stage}"
    rel = _rel_path(args.stage_dir, "--stage-dir")
    match = _STAGE_RE.match(rel)
    return (int(match.group(1)), rel) if match else (1, rel)


def _scripts(snap: Path, rel_dir: str) -> list[str]:
    directory = snap / rel_dir
    if not directory.is_dir():
        return []
    return sorted(p.relative_to(snap).as_posix() for p in directory.glob("check_*.py"))


def _step_kind(result: CheckResult) -> str:
    if result.ok:
        return "pass"
    return "fail" if result.name in ("build", "healthy") else "infra"


def _script_kind(result: CheckResult) -> str:
    if result.ok:
        return "pass"
    return "fail" if result.exit == 1 else "infra"


def _record(checks: list[CheckResult], kinds: list[str], result: CheckResult, label: str, kind: str) -> None:
    result = replace(result, name=label)
    checks.append(result)
    kinds.append(kind)
    print(f"{'PASS' if result.ok else 'FAIL'} {label} ({result.duration_s:.1f}s)", flush=True)
    if not result.ok:
        print(f"--- {label} ({kind}, exit {result.exit}) ---\n{result.stdout_tail}\n---", file=sys.stderr, flush=True)


def _promotable(args: argparse.Namespace) -> int:
    sha = git("rev-parse", "--verify", f"{args.commit}^{{commit}}")
    ok = promotable(Path(args.evidence_root), args.promotable, sha)
    print(f"{'PROMOTABLE' if ok else 'NOT_PROMOTABLE'} stage-{args.promotable} @{sha[:7]}")
    return 0 if ok else 1


def _run(args: argparse.Namespace) -> int:
    cfg = load_config()
    evroot = Path(args.evidence_root)
    if args.repeat < 1:
        raise ValueError(f"--repeat must be >= 1, got {args.repeat}.")
    stage, stage_rel = _resolve_stage(args)
    env_overrides = _parse_env(args.env)
    acceptance_rel = _rel_path(args.acceptance, "--acceptance") if args.acceptance else f"{stage_rel}/acceptance"
    # Fail fast on an invalid item or seat name, before the expensive build.
    evidence_path(evroot, Evidence(SCHEMA_VERSION, stage, args.item, args.seat, args.kind, "0000000", "", "", [], "infra"))
    sha = git("rev-parse", "--verify", f"{args.commit}^{{commit}}")
    cache = args.cache or cfg.build_cache
    inherit = (not args.no_inherit) and stage > 1 and stage_rel == f"stage-{stage}"

    started_at = now_iso()
    clock = time.monotonic()
    checks: list[CheckResult] = []
    kinds: list[str] = []
    applied: set[str] = set()
    ratchet_ran = False

    with tempfile.TemporaryDirectory(prefix="factory-gate-") as tmp:
        snap = Path(tmp) / "repo"
        snapshot(sha, snap)
        stage_dir = snap / stage_rel
        if not stage_dir.is_dir():
            raise FileNotFoundError(f"{stage_rel}/ does not exist at commit {sha[:7]}. Commit the stage directory first.")
        spec = load_spec(stage_dir, env_overrides)
        waived = {w["id"] for w in spec.waivers}
        own = _scripts(snap, acceptance_rel)

        with Sandbox(stage_dir, spec, f"{args.kind}-{sha[:7]}", cache) as sandbox:
            for step in list(sandbox.results):
                _record(checks, kinds, step, step.name, _step_kind(step))
            if sandbox.ok:
                if not own:
                    missing = CheckResult(
                        "acceptance_present", f"ls {acceptance_rel}/check_*.py", 1, 0.0, False,
                        f"No check_*.py found in {acceptance_rel}/ at commit {sha[:7]}. Every item needs a falsifiable check.",
                    )
                    _record(checks, kinds, missing, "acceptance_present", "fail")
                else:
                    for i in range(args.repeat):
                        seed = args.seed + i
                        suffix = f"@seed{seed}" if args.repeat > 1 else ""
                        run_env = {"SEED": str(seed)}
                        for script in own:
                            result = sandbox.run_check(snap, [script], run_env, CHECK_TIMEOUT_S)
                            _record(checks, kinds, result, f"{Path(script).name}{suffix}", _script_kind(result))
                        if inherit:
                            for k in range(1, stage):
                                for script in _scripts(snap, f"stage-{k}/acceptance"):
                                    wid = f"stage-{k}/{Path(script).name}"
                                    if wid in waived:
                                        applied.add(wid)
                                        print(f"WAIVED {wid}", file=sys.stderr)
                                        continue
                                    result = sandbox.run_check(snap, [script], run_env, CHECK_TIMEOUT_S)
                                    _record(checks, kinds, result, f"{wid}{suffix}", _script_kind(result))
                if not args.no_ratchet:
                    ratchet_dir = snap / "ratchet"
                    has_cx = ratchet_dir.is_dir() and any(ratchet_dir.glob("CX-*.json"))
                    if has_cx:
                        argv = ["factory/bin/ratchet.py", "check", "--base-url", sandbox.base_url]
                        for wid in sorted(w for w in waived if _CX_RE.match(w)):
                            argv += ["--waive", wid]
                            applied.add(wid)
                        result = sandbox.run_check(snap, argv, {"SEED": str(args.seed)}, RATCHET_TIMEOUT_S)
                        _record(checks, kinds, result, "ratchet", _script_kind(result))
                        ratchet_ran = True
                    else:
                        print("NOTE no ratchet/CX-*.json at this commit; ratchet step skipped", file=sys.stderr)

    if "fail" in kinds:
        verdict = "fail"
    elif "infra" in kinds:
        verdict = "infra"
    else:
        verdict = "pass"

    prior, _ = rework_cycle(evroot, args.item, cfg.max_rework)
    cycle = prior + 1 if verdict == "fail" else (0 if verdict == "pass" else prior)
    exhausted = verdict == "fail" and cycle >= cfg.max_rework
    evidence = Evidence(
        schema=SCHEMA_VERSION,
        stage=stage,
        item=args.item,
        seat=args.seat,
        kind=args.kind,
        commit=sha,
        started_at=started_at,
        finished_at=now_iso(),
        checks=checks,
        verdict=verdict,
        counterexample_id=None,
        resources={
            "acceptance_dir": acceptance_rel,
            "build_network": spec.build_network,
            "cache": cache,
            "cpus": spec.cpus,
            "driver_image": cfg.driver_image,
            "inherit": inherit,
            "memory": spec.memory,
            "ratchet_ran": ratchet_ran,
            "repeat": args.repeat,
            "seed": args.seed,
            "stage_dir": stage_rel,
            "start_timeout_s": spec.start_timeout_s,
            "wall_clock_s": round(time.monotonic() - clock, 3),
        },
        rework_cycle=cycle,
        budget_exhausted=exhausted,
        waivers=[{"id": w["id"], "reason": w["reason"], "applied": w["id"] in applied} for w in spec.waivers],
        room_ref=None,
    )
    path = write_evidence(evroot, evidence)
    if exhausted:
        print(
            f"BUDGET_EXHAUSTED item {args.item}: {cycle} consecutive failures (limit {cfg.max_rework}). "
            "The Steward must revert to green and the Planner must re-slice.",
            file=sys.stderr,
        )
    print(f"EVIDENCE {path}")
    return {"pass": 0, "fail": 1, "infra": 2}[verdict]


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.promotable is not None:
            return _promotable(args)
        if not args.item or not args.seat or (args.stage is None and args.stage_dir is None):
            parser.error("--item, --seat and one of --stage/--stage-dir are required (unless --promotable is used)")
        return _run(args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"INFRA ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
