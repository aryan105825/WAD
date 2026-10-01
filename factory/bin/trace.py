# @exports: main(argv: list[str] | None = None) -> int   # exit 0 ok | 1 coverage/authorship failure | 2 usage/infra
# @exports: load_room(path: Path, fmt: str) -> tuple[str, str]   # (searchable text, format_used)
# @imports: factory/lib/common.py:git(*args, cwd=None), head_commit(), write_json(path, obj)
# @env: none
# @schema: trace.py --since REF --stages N... --room-export PATH [--format auto|text] [--min-coverage 1.0] [--out results/trace.json]
# @schema: per commit in REF..HEAD touching stage-N/: needs trailers Item, Seat, Room-Ref; traceable iff all present AND Item id (or Room-Ref if not "-") appears in the room export
# @schema: --format auto: JSON/JSONL export is flattened to its string values and ids match on word boundaries; anything else is raw text (same matching). --format text: raw substring grep
# @schema: fail iff commits == 0 OR coverage < min OR any such commit author name not matching ^seat-
# @schema: out = {"commits","traceable","coverage","human_authored":[sha],"unmatched":[sha],"missing_trailers":[sha],"since","head","stages","format","min_coverage","ok"}
"""Traceability: every stage commit must trace to the room and be authored by a seat."""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from factory.lib.common import git, head_commit, write_json  # noqa: E402

REC = "\x1e"
FLD = "\x1f"
TRAILERS = {
    "Item": re.compile(r"^Item:[ \t]*(\S.*?)[ \t]*$", re.M),
    "Seat": re.compile(r"^Seat:[ \t]*(\S.*?)[ \t]*$", re.M),
    "Room-Ref": re.compile(r"^Room-Ref:[ \t]*(\S.*?)[ \t]*$", re.M),
}
SEAT_AUTHOR = re.compile(r"^seat-")


def die(msg: str, code: int = 2):
    print(f"trace: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif obj is not None:
        yield str(obj)


def _load_one(path: Path, fmt: str) -> tuple[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        die(f"cannot read room export {path}: {exc}. Re-export it as UTF-8 text/JSON.")
    if fmt == "text":
        return raw, "text"
    # auto: a failed JSON parse is the normal signal that the export is not JSON, not an error.
    try:
        return "\n".join(_strings(json.loads(raw))), "json"
    except ValueError:
        pass
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if lines:
        try:
            return "\n".join(s for ln in lines for s in _strings(json.loads(ln))), "jsonl"
        except ValueError:
            pass
    return raw, "text"


def load_room(path: Path, fmt: str) -> tuple[str, str]:
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
        if not files:
            die(f"room export directory {path} is empty. Export the room recording/transcript into it.")
        parts, used = [], set()
        for f in files:
            text, u = _load_one(f, fmt)
            parts.append(text)
            used.add(u)
        return "\n".join(parts), "+".join(sorted(used))
    if not path.is_file():
        die(f"room export {path} not found. Export the room transcript and pass its path.")
    return _load_one(path, fmt)


def appears(needle: str, blob: str, fmt: str) -> bool:
    if fmt == "text":
        return needle in blob
    return re.search(r"(?<![\w-])" + re.escape(needle) + r"(?![\w-])", blob) is not None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trace.py")
    ap.add_argument("--since", required=True)
    ap.add_argument("--stages", nargs="+", type=int, required=True)
    ap.add_argument("--room-export", required=True)
    ap.add_argument("--format", choices=["auto", "text"], default="auto")
    ap.add_argument("--min-coverage", type=float, default=1.0)
    ap.add_argument("--out", default="results/trace.json")
    a = ap.parse_args(argv)

    def resolve_path(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else ROOT / q

    blob, fmt_used = load_room(resolve_path(a.room_export), a.format)
    match_mode = "text" if a.format == "text" else "auto"

    paths = [f"stage-{n}" for n in a.stages]
    try:
        raw = git(
            "log",
            f"{a.since}..HEAD",
            f"--format=%H{FLD}%an{FLD}%B{REC}",
            "--",
            *paths,
            cwd=ROOT,
        )
        head = head_commit()
    except RuntimeError as exc:
        die(f"git failed: {exc}. Check that --since '{a.since}' exists (e.g. tag factory-start).")

    commits = traceable = 0
    human: list[str] = []
    unmatched: list[str] = []
    missing: list[str] = []
    for rec in raw.split(REC):
        if not rec.strip():
            continue
        sha, author, body = rec.lstrip("\n").split(FLD, 2)
        sha = sha.strip()
        commits += 1
        if not SEAT_AUTHOR.match(author.strip()):
            human.append(sha)
        found = {k: rx.search(body) for k, rx in TRAILERS.items()}
        if any(m is None for m in found.values()):
            missing.append(sha)
            unmatched.append(sha)
            continue
        item = found["Item"].group(1)
        room_ref = found["Room-Ref"].group(1)
        hit = appears(item, blob, match_mode) or (room_ref != "-" and appears(room_ref, blob, match_mode))
        if hit:
            traceable += 1
        else:
            unmatched.append(sha)

    coverage = (traceable / commits) if commits else 0.0
    ok = commits > 0 and coverage >= a.min_coverage and not human
    out = resolve_path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        out,
        {
            "commits": commits,
            "coverage": coverage,
            "format": fmt_used,
            "head": head,
            "human_authored": human,
            "min_coverage": a.min_coverage,
            "missing_trailers": missing,
            "ok": ok,
            "since": a.since,
            "stages": a.stages,
            "traceable": traceable,
            "unmatched": unmatched,
        },
    )
    print(
        f"trace: {commits} commits, {traceable} traceable ({coverage:.0%}), "
        f"{len(human)} human-authored, {len(missing)} missing trailers"
    )
    if commits == 0:
        print(f"FIX: no commits touching {paths} since {a.since}; check --since/--stages.", file=sys.stderr)
    for sha in human:
        print(f"HUMAN-AUTHORED {sha[:7]}: stage commits must be authored by a seat-* git identity")
    for sha in unmatched:
        print(f"UNMATCHED {sha[:7]}: add Item/Seat/Room-Ref trailers and post the item id in the room")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
