# @exports: main(argv: list[str] | None = None) -> int   # CLI entry; exit 0 ok | 1 violated/failed | 2 infra
# @exports: load_all() -> list[dict]   # every ratchet/CX-*.json, sorted by id
# @exports: replay_one(cx_id: str, commit: str, cache: bool = False) -> tuple[bool, str]   # (violated, full_sha); raises InfraError
# @exports: InfraError(Exception)
# @imports: factory/lib/common.py:SCHEMA_VERSION, Counterexample, git(*args, cwd=None), head_commit(), read_json(path), snapshot(commit, dest), write_json(path, obj)
# @imports: factory/lib/sandbox.py:load_spec(stage_dir), build(tree, spec, tag, cache), start(tag, name, spec), wait_healthy(name, spec), run_driver(name, repo_snapshot, argv, env, timeout_s), stop(name)
# @env: BASE_URL, SEED   (only WRITTEN into runner subprocess/driver env; never read from the caller's env)
# @schema: ratchet.py add --id CX-0001 --stage N --found-at SHA --title T --invariant TEXT --runner ratchet/cx/CX-0001.py --seed INT --seat S [--room-ref R]
# @schema: ratchet.py fixed --id CX-0001 --fixed-at SHA
# @schema: ratchet.py list [--json]
# @schema: ratchet.py check --base-url URL [--waive ID ...]   # prints one JSON line per counterexample {"detail","name","ok"}; exit 1 if any violated
# @schema: ratchet.py replay --id CX-0001 --commit SHA [--cache]   # prints "VIOLATED|HELD <id> @<sha7>"; exit 0 held, 1 violated, 2 infra
# @schema: ratchet.py verify-history [--max 3] [--out results/ratchet.json] [--cache]   # exit 1 unless VIOLATED@found_at AND HELD@fixed_at for every checked CX
# @schema: ratchet/{CX-NNNN}.json = Counterexample{schema,id,stage_found,found_at,fixed_at,title,invariant,runner,seed,created_by,room_ref}
# @schema: runner contract: env BASE_URL, SEED; exit 1 on violation; last stdout line {"violated": bool, "detail": str}
# @schema: runner kinds: "hammer" (direct check) | "differential" (runner itself loads ratchet/models/<id>_model.py; replay copies that file into the snapshot)
# @schema: results/ratchet.json = {"commit","max","checked":[{"id","found_at","fixed_at","at_found","at_fixed","ok"}],"open":[id],"ok":bool}
"""Replayable counterexample ratchet.

Counterexamples live in ratchet/CX-NNNN.json. Each one must be VIOLATED at its
found_at commit and HELD at its fixed_at commit, and HELD in every later stage.
"""
import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from factory.lib import sandbox  # noqa: E402
from factory.lib.common import (  # noqa: E402
    SCHEMA_VERSION,
    Counterexample,
    git,
    head_commit,
    read_json,
    snapshot,
    write_json,
)

ID_RE = re.compile(r"^CX-\d{4}$")
RUNNER_TIMEOUT_S = 120


class InfraError(Exception):
    """Environment/tooling problem (not a verdict about the product)."""


def die(msg: str, code: int = 2):
    print(f"ratchet: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def cx_path(cx_id: str) -> Path:
    return ROOT / "ratchet" / f"{cx_id}.json"


def load_all() -> list[dict]:
    d = ROOT / "ratchet"
    if not d.is_dir():
        return []
    return [read_json(p) for p in sorted(d.glob("CX-*.json"))]


def load_one(cx_id: str) -> dict:
    if not ID_RE.match(cx_id):
        die(f"bad counterexample id '{cx_id}'. Expected CX-NNNN (four digits).")
    p = cx_path(cx_id)
    if not p.is_file():
        die(f"{p.relative_to(ROOT)} not found. Run 'ratchet.py list' to see known ids.")
    return read_json(p)


def resolve(ref: str) -> str:
    try:
        return git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=ROOT).strip()
    except RuntimeError as exc:
        die(f"cannot resolve '{ref}' to a commit: {exc}. Pass a SHA that exists in this repo.")


def interpret(exit_code: int, stdout: str) -> tuple[bool, str]:
    """Apply the runner contract. Returns (violated, detail) or raises InfraError."""
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise InfraError(
            'runner printed nothing. Its last stdout line must be {"violated": bool, "detail": str}.'
        )
    try:
        obj = json.loads(lines[-1])
    except ValueError as exc:
        raise InfraError(f"runner's last stdout line is not JSON ({exc}): {lines[-1][:200]!r}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("violated"), bool):
        raise InfraError(f'runner result must be {{"violated": bool, "detail": str}}, got: {lines[-1][:200]!r}')
    detail = str(obj.get("detail", ""))
    if exit_code == 1 and obj["violated"]:
        return True, detail
    if exit_code == 0 and not obj["violated"]:
        return False, detail
    raise InfraError(
        f"runner exit code {exit_code} disagrees with violated={obj['violated']}. "
        "Runner must exit 1 iff violated, else 0 (a crash is infra, not a verdict)."
    )


def replay_one(cx_id: str, commit: str, cache: bool = False) -> tuple[bool, str]:
    cx = load_one(cx_id)
    sha = resolve(commit)
    runner_rel = cx["runner"]
    runner_src = ROOT / runner_rel
    if not runner_src.is_file():
        raise InfraError(f"runner {runner_rel} for {cx_id} is missing from the working tree.")
    tmp = Path(tempfile.mkdtemp(prefix="ratchet-replay-"))
    name = ""
    started = False
    try:
        snap = tmp / "repo"
        snap.mkdir()
        snapshot(sha, snap)
        # The runner/model may not exist yet at the old commit: overlay them from HEAD.
        overlay = [(runner_src, snap / runner_rel)]
        model_rel = Path("ratchet") / "models" / f"{cx_id}_model.py"
        if (ROOT / model_rel).is_file():
            overlay.append((ROOT / model_rel, snap / model_rel))
        for src, dst in overlay:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        stage_dir = snap / f"stage-{cx['stage_found']}"
        if not stage_dir.is_dir():
            raise InfraError(
                f"stage-{cx['stage_found']}/ does not exist at {sha[:7]}. "
                "Replay the counterexample at a commit that contains its stage directory."
            )
        spec = sandbox.load_spec(stage_dir)
        tag = f"ratchet-{cx_id.lower()}-{sha[:7]}"
        name = f"{tag}-{os.getpid()}"
        res = sandbox.build(stage_dir, spec, tag, cache)
        if not res.ok:
            raise InfraError(f"docker build failed at {sha[:7]}: {res.stdout_tail}")
        started = True
        res = sandbox.start(tag, name, spec)
        if not res.ok:
            raise InfraError(f"container start failed at {sha[:7]}: {res.stdout_tail}")
        res = sandbox.wait_healthy(name, spec)
        if not res.ok:
            raise InfraError(
                f"service not healthy at {sha[:7]} within {spec.start_timeout_s}s: {res.stdout_tail}"
            )
        res = sandbox.run_driver(
            name,
            snap,
            ["/repo/" + runner_rel],
            {"BASE_URL": f"http://127.0.0.1:{spec.port}", "SEED": str(cx["seed"])},
            RUNNER_TIMEOUT_S,
        )
        violated, _detail = interpret(res.exit, res.stdout_tail)
        return violated, sha
    finally:
        if started:
            sandbox.stop(name)
        shutil.rmtree(tmp, ignore_errors=True)


def check_one(cx: dict, base_url: str) -> tuple[bool, str]:
    runner = ROOT / cx["runner"]
    if not runner.is_file():
        return False, f"runner {cx['runner']} missing"
    env = dict(os.environ)
    env["BASE_URL"] = base_url
    env["SEED"] = str(cx["seed"])
    env["PYTHONPATH"] = str(ROOT)
    try:
        proc = subprocess.run(
            [sys.executable, str(runner)],
            env=env,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=RUNNER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"runner timed out after {RUNNER_TIMEOUT_S}s"
    try:
        violated, detail = interpret(proc.returncode, proc.stdout)
    except InfraError as exc:
        return False, f"runner contract error: {exc}; stderr tail: {proc.stderr[-300:]}"
    return (not violated), detail


# ---- subcommands -----------------------------------------------------------------

def cmd_add(a) -> int:
    if not ID_RE.match(a.id):
        die(f"bad id '{a.id}'. Expected CX-NNNN (four digits).")
    path = cx_path(a.id)
    if path.exists():
        die(f"{a.id} already exists. Counterexamples are permanent; choose a new id.")
    runner_abs = Path(a.runner)
    if not runner_abs.is_absolute():
        runner_abs = ROOT / runner_abs
    if not runner_abs.is_file():
        die(f"runner {a.runner} not found. Write the runner script before registering the counterexample.")
    try:
        rel = runner_abs.resolve().relative_to(ROOT)
    except ValueError:
        die(f"runner {a.runner} is outside the repo; place it under ratchet/cx/.")
    found = resolve(a.found_at)
    cx = Counterexample(
        schema=SCHEMA_VERSION,
        id=a.id,
        stage_found=a.stage,
        found_at=found,
        fixed_at=None,
        title=a.title,
        invariant=a.invariant,
        runner=rel.as_posix(),
        seed=a.seed,
        created_by=a.seat,
        room_ref=a.room_ref,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, dataclasses.asdict(cx))
    print(f"ADDED {a.id} stage={a.stage} found_at={found[:7]}")
    return 0


def cmd_fixed(a) -> int:
    cx = load_one(a.id)
    if cx.get("fixed_at"):
        die(f"{a.id} is already marked fixed at {cx['fixed_at'][:7]}.", code=1)
    cx["fixed_at"] = resolve(a.fixed_at)
    write_json(cx_path(a.id), cx)
    print(f"FIXED {a.id} fixed_at={cx['fixed_at'][:7]}")
    return 0


def cmd_list(a) -> int:
    cxs = load_all()
    if a.json:
        print(json.dumps(cxs, indent=2, sort_keys=True))
        return 0
    if not cxs:
        print("no counterexamples recorded")
        return 0
    for cx in cxs:
        state = f"fixed@{cx['fixed_at'][:7]}" if cx.get("fixed_at") else "open"
        print(f"{cx['id']}  stage={cx['stage_found']}  found@{cx['found_at'][:7]}  {state}  {cx['title']}")
    return 0


def cmd_check(a) -> int:
    cxs = load_all()
    waive = set(a.waive)
    unknown = waive - {c["id"] for c in cxs}
    if unknown:
        die(f"--waive names unknown counterexample(s): {sorted(unknown)}. Check 'ratchet.py list'.")
    failed = 0
    waived = 0
    for cx in cxs:
        if cx["id"] in waive:
            waived += 1
            print(json.dumps({"detail": "waived", "name": cx["id"], "ok": True}, sort_keys=True))
            continue
        ok, detail = check_one(cx, a.base_url)
        if not ok:
            failed += 1
        print(json.dumps({"detail": detail, "name": cx["id"], "ok": ok}, sort_keys=True))
    print(f"ratchet check: {len(cxs)} counterexamples, {failed} failed, {waived} waived")
    return 1 if failed else 0


def cmd_replay(a) -> int:
    try:
        violated, sha = replay_one(a.id, a.commit, a.cache)
    except InfraError as exc:
        die(f"replay of {a.id} could not run: {exc}")
    print(f"{'VIOLATED' if violated else 'HELD'} {a.id} @{sha[:7]}")
    return 1 if violated else 0


def cmd_verify_history(a) -> int:
    cxs = load_all()
    fixed = [c for c in cxs if c.get("fixed_at")]
    open_ids = [c["id"] for c in cxs if not c.get("fixed_at")]
    if not fixed:
        die(
            "no fixed counterexamples to verify. Record at least one with 'ratchet.py fixed' first.",
            code=1,
        )
    checked = []
    for cx in fixed[: a.max]:
        try:
            v_found, _ = replay_one(cx["id"], cx["found_at"], a.cache)
            v_fixed, _ = replay_one(cx["id"], cx["fixed_at"], a.cache)
        except InfraError as exc:
            die(f"history replay of {cx['id']} could not run: {exc}")
        ok = v_found and not v_fixed
        checked.append(
            {
                "id": cx["id"],
                "found_at": cx["found_at"],
                "fixed_at": cx["fixed_at"],
                "at_found": "VIOLATED" if v_found else "HELD",
                "at_fixed": "VIOLATED" if v_fixed else "HELD",
                "ok": ok,
            }
        )
        print(
            f"{'OK ' if ok else 'BAD'} {cx['id']} found@{cx['found_at'][:7]}={checked[-1]['at_found']} "
            f"fixed@{cx['fixed_at'][:7]}={checked[-1]['at_fixed']}"
        )
    all_ok = all(c["ok"] for c in checked)
    try:
        commit = head_commit()
    except RuntimeError as exc:
        die(f"cannot read HEAD to cite in results: {exc}. Make at least one commit first.")
    out = Path(a.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, {"checked": checked, "commit": commit, "max": a.max, "ok": all_ok, "open": open_ids})
    return 0 if all_ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ratchet.py", description="Replayable counterexample ratchet")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add")
    s.add_argument("--id", required=True)
    s.add_argument("--stage", type=int, required=True)
    s.add_argument("--found-at", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--invariant", required=True)
    s.add_argument("--runner", required=True)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--seat", required=True)
    s.add_argument("--room-ref", default=None)
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("fixed")
    s.add_argument("--id", required=True)
    s.add_argument("--fixed-at", required=True)
    s.set_defaults(fn=cmd_fixed)

    s = sub.add_parser("list")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("check")
    s.add_argument("--base-url", required=True)
    s.add_argument("--waive", nargs="+", default=[])
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("replay")
    s.add_argument("--id", required=True)
    s.add_argument("--commit", required=True)
    s.add_argument("--cache", action="store_true")
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("verify-history")
    s.add_argument("--max", type=int, default=3)
    s.add_argument("--out", default="results/ratchet.json")
    s.add_argument("--cache", action="store_true")
    s.set_defaults(fn=cmd_verify_history)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
