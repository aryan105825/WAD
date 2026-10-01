# @exports: main(argv: list[str] | None = None) -> int   # exit 0 ok | 1 budget violation (--assert-budget) | 2 usage/infra
# @exports: compute(evidence_root: Path, max_rework: int) -> dict   # {"stages":[...], "budget_violations":[...]}
# @exports: render_block(stages: list[dict]) -> str   # markdown table injected between the COST markers
# @imports: factory/lib/common.py:head_commit(), read_json(path), write_json(path, obj)
# @env: none   (max_rework comes from seats.json["max_rework"])
# @schema: costreport.py [--evidence evidence] [--out results/cost.json] [--inject FACTORY.md] [--assert-budget]
# @schema: out = {"commit","max_rework","budget_violations":[{"stage","item","reason"}],"stages":[{"stage","wall_clock_s","gate_runs","gate_failures","adversarial_runs","rework_cycles","reverts","counterexamples","loc","waivers"}]}
# @schema: evidence read: evidence/stage-N/<item>/NNN-<seat>-<kind>.json (fields used: kind, verdict, started_at, finished_at, rework_cycle, budget_exhausted, waivers)
# @schema: wall_clock_s = latest finished_at minus earliest started_at in the stage; gate_failures = kind=gate verdict=fail; rework_cycles = sum over items of max rework_cycle; waivers = distinct waiver ids; loc = lines in source-like files under stage-N/
# @schema: budget rule: per item, streak of consecutive kind=gate verdict=fail (infra ignored, pass resets) reaching max_rework, or budget_exhausted=true, must be followed by a kind=revert evidence
# @schema: --inject replaces everything between <!-- COST:BEGIN --> and <!-- COST:END --> (both markers required, exactly once)
"""Aggregate factory evidence into measured cost/time numbers."""
import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from factory.lib.common import head_commit, read_json, write_json  # noqa: E402

BEGIN = "<!-- COST:BEGIN -->"
END = "<!-- COST:END -->"
STAGE_RE = re.compile(r"^stage-(\d+)$")
SRC_SUFFIXES = {".py", ".js", ".ts", ".html", ".css", ".sql", ".sh", ".json", ".md", ".txt", ".yml", ".yaml"}
REQUIRED = ("kind", "verdict", "started_at", "finished_at")


def die(msg: str, code: int = 2):
    print(f"costreport: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_ts(value: str, where: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        die(f"bad timestamp {value!r} in {where}. Expected ISO-8601; regenerate the evidence with gate.py.")
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def load_evidence(root: Path) -> dict[int, dict[str, list[dict]]]:
    if not root.is_dir():
        die(f"evidence directory {root} not found. Run the factory first, or pass --evidence.")
    stages: dict[int, dict[str, list[dict]]] = {}
    for sd in sorted(root.iterdir()):
        m = STAGE_RE.match(sd.name)
        if not m or not sd.is_dir():
            continue
        items: dict[str, list[dict]] = {}
        for idir in sorted(p for p in sd.iterdir() if p.is_dir()):
            evs = []
            for f in sorted(idir.glob("*.json")):
                ev = read_json(f)
                missing = [k for k in REQUIRED if k not in ev]
                if missing:
                    die(f"{f} is missing evidence fields {missing}. It was not written by evidence.write_evidence.")
                evs.append(ev)
            if evs:
                items[idir.name] = evs
        stages[int(m.group(1))] = items
    if not stages:
        die(f"no stage-*/ evidence under {root}. Nothing to report.")
    return stages


def count_loc(stage: int) -> int:
    d = ROOT / f"stage-{stage}"
    if not d.is_dir():
        return 0
    total = 0
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SRC_SUFFIXES:
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(d).parts):
            continue
        try:
            total += len(p.read_text(encoding="utf-8").splitlines())
        except UnicodeDecodeError as exc:
            die(f"{p} is not valid UTF-8 ({exc}); remove it or store it outside stage-{stage}/.")
    return total


def counterexamples_by_stage() -> dict[int, int]:
    out: dict[int, int] = {}
    d = ROOT / "ratchet"
    if d.is_dir():
        for f in sorted(d.glob("CX-*.json")):
            n = int(read_json(f)["stage_found"])
            out[n] = out.get(n, 0) + 1
    return out


def budget_violations(stage: int, items: dict[str, list[dict]], max_rework: int) -> list[dict]:
    bad = []
    for item, evs in items.items():
        streak = 0
        pending = False
        for ev in evs:
            kind, verdict = ev["kind"], ev["verdict"]
            if kind == "revert":
                streak = 0
                pending = False
                continue
            if ev.get("budget_exhausted"):
                pending = True
            if kind != "gate":
                continue
            if verdict == "fail":
                streak += 1
                if streak >= max_rework:
                    pending = True
            elif verdict == "pass":
                streak = 0
        if pending:
            bad.append(
                {
                    "item": item,
                    "reason": f"rework budget ({max_rework}) exhausted with no later kind=revert evidence",
                    "stage": stage,
                }
            )
    return bad


def compute(evidence_root: Path, max_rework: int) -> dict:
    evidence = load_evidence(evidence_root)
    cxs = counterexamples_by_stage()
    stages, violations = [], []
    for n in sorted(evidence):
        items = evidence[n]
        all_ev = [ev for evs in items.values() for ev in evs]
        starts = [parse_ts(e["started_at"], f"stage-{n}") for e in all_ev]
        ends = [parse_ts(e["finished_at"], f"stage-{n}") for e in all_ev]
        waiver_ids = set()
        for e in all_ev:
            for w in e.get("waivers") or []:
                waiver_ids.add(str(w.get("id")) if isinstance(w, dict) else str(w))
        stages.append(
            {
                "adversarial_runs": sum(1 for e in all_ev if e["kind"] == "adversarial"),
                "counterexamples": cxs.get(n, 0),
                "gate_failures": sum(1 for e in all_ev if e["kind"] == "gate" and e["verdict"] == "fail"),
                "gate_runs": sum(1 for e in all_ev if e["kind"] == "gate"),
                "loc": count_loc(n),
                "reverts": sum(1 for e in all_ev if e["kind"] == "revert"),
                "rework_cycles": sum(max(int(e.get("rework_cycle") or 0) for e in evs) for evs in items.values()),
                "stage": n,
                "wall_clock_s": round((max(ends) - min(starts)).total_seconds(), 1),
                "waivers": len(waiver_ids),
            }
        )
        violations += budget_violations(n, items, max_rework)
    return {"budget_violations": violations, "stages": stages}


def render_block(stages: list[dict]) -> str:
    lines = [
        "| Stage | Wall clock (s) | Gate runs | Gate failures | Adversarial runs | Rework cycles | Reverts | Counterexamples | LOC | Waivers |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in stages:
        lines.append(
            f"| {s['stage']} | {s['wall_clock_s']:.1f} | {s['gate_runs']} | {s['gate_failures']} | "
            f"{s['adversarial_runs']} | {s['rework_cycles']} | {s['reverts']} | {s['counterexamples']} | "
            f"{s['loc']} | {s['waivers']} |"
        )
    return "\n".join(lines)


def inject(path: Path, block: str) -> None:
    if not path.is_file():
        die(f"{path} not found; cannot inject the cost block.")
    text = path.read_text(encoding="utf-8")
    if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) > text.index(END):
        die(f"{path} must contain exactly one {BEGIN} followed by exactly one {END}.")
    head, rest = text.split(BEGIN, 1)
    _old, tail = rest.split(END, 1)
    path.write_text(f"{head}{BEGIN}\n{block}\n{END}{tail}", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="costreport.py")
    ap.add_argument("--evidence", default="evidence")
    ap.add_argument("--out", default="results/cost.json")
    ap.add_argument("--inject", default=None)
    ap.add_argument("--assert-budget", action="store_true")
    a = ap.parse_args(argv)

    def resolve_path(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else ROOT / q

    seats = read_json(ROOT / "seats.json")
    if "max_rework" not in seats:
        die("seats.json has no 'max_rework'. Add it (contract default 3).")
    max_rework = int(seats["max_rework"])

    report = compute(resolve_path(a.evidence), max_rework)
    try:
        commit = head_commit()
    except RuntimeError as exc:
        die(f"cannot read HEAD to cite in results: {exc}. Make at least one commit first.")
    report["commit"] = commit
    report["max_rework"] = max_rework

    out = resolve_path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(out, report)
    print(f"costreport: {len(report['stages'])} stages -> {a.out}")

    if a.inject:
        inject(resolve_path(a.inject), render_block(report["stages"]))
        print(f"costreport: injected cost block into {a.inject}")

    if a.assert_budget:
        for v in report["budget_violations"]:
            print(f"FAIL stage-{v['stage']}/{v['item']}: {v['reason']}")
        if report["budget_violations"]:
            print("FIX: the Steward must write a kind=revert evidence (revert to green tag) after the budget is exhausted.")
            return 1
        print(f"PASS budget: every item at or over {max_rework} consecutive failures was reverted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
