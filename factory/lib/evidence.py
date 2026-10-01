# @exports: KINDS: tuple[str, ...]  # gate, adversarial, review, promotion, revert
# @exports: VERDICTS: tuple[str, ...]  # pass, fail, infra
# @exports: evidence_path(root: Path, ev: Evidence) -> Path
# @exports: next_seq(d: Path) -> int
# @exports: iter_evidence(root: Path, stage: int|None=None, item: str|None=None) -> list[tuple[Path, dict]]
# @exports: rework_cycle(root: Path, item: str, max_rework: int) -> tuple[int, bool]
# @exports: write_evidence(root: Path, ev: Evidence) -> Path
# @exports: promotable(root: Path, stage: int, commit: str) -> bool
# @imports: factory/lib/common.py:SCHEMA_VERSION, Evidence, CheckResult, read_json, write_json
# @env: none
# @schema: evidence file path: <root>/stage-{N}/{item}/{seq:03d}-{seat}-{kind}.json
# @schema: root is the evidence root directory itself (default "evidence"), not the repo root
# @schema: rework streak = consecutive fail verdicts of kind gate|adversarial, newest first; pass/promotion/revert end it; infra is skipped
# @schema: promotable = a pass gate record AND a pass adversarial record for the same commit (prefix match, >= 7 chars) in that stage
from __future__ import annotations

import re
from pathlib import Path

from factory.lib.common import SCHEMA_VERSION, CheckResult, Evidence, read_json, write_json

KINDS = ("gate", "adversarial", "review", "promotion", "revert")
VERDICTS = ("pass", "fail", "infra")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SEQ_RE = re.compile(r"^(\d{3})-")
_STAGE_DIR_RE = re.compile(r"^stage-(\d+)$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_CX_RE = re.compile(r"^CX-\d{4}$")


def _validate_names(ev: Evidence) -> None:
    if not isinstance(ev.stage, int) or isinstance(ev.stage, bool) or ev.stage < 1:
        raise ValueError(f"evidence stage must be an integer >= 1, got {ev.stage!r}.")
    for label, value in (("item", ev.item), ("seat", ev.seat)):
        if not isinstance(value, str) or not _NAME_RE.match(value):
            raise ValueError(
                f"evidence {label} {value!r} is invalid: use letters, digits, '_', '.', '-' and start with a letter or digit."
            )
    if ev.kind not in KINDS:
        raise ValueError(f"evidence kind {ev.kind!r} is invalid; use one of {KINDS}.")


def next_seq(d: Path) -> int:
    d = Path(d)
    if not d.exists():
        return 1
    highest = 0
    for entry in d.iterdir():
        match = _SEQ_RE.match(entry.name)
        if match and entry.suffix == ".json":
            highest = max(highest, int(match.group(1)))
    return highest + 1


def evidence_path(root: Path, ev: Evidence) -> Path:
    _validate_names(ev)
    directory = Path(root) / f"stage-{ev.stage}" / ev.item
    return directory / f"{next_seq(directory):03d}-{ev.seat}-{ev.kind}.json"


def iter_evidence(root: Path, stage: int | None = None, item: str | None = None) -> list[tuple[Path, dict]]:
    """All evidence records under root, ordered by stage, item, then file name (sequence)."""
    root = Path(root)
    if not root.is_dir():
        return []
    stage_dirs: list[tuple[int, Path]] = []
    for entry in root.iterdir():
        match = _STAGE_DIR_RE.match(entry.name)
        if match and entry.is_dir() and (stage is None or int(match.group(1)) == stage):
            stage_dirs.append((int(match.group(1)), entry))
    records: list[tuple[Path, dict]] = []
    for _, stage_dir in sorted(stage_dirs, key=lambda pair: pair[0]):
        for item_dir in sorted(p for p in stage_dir.iterdir() if p.is_dir()):
            if item is not None and item_dir.name != item:
                continue
            for file in sorted(item_dir.glob("*.json")):
                records.append((file, read_json(file)))
    return records


def rework_cycle(root: Path, item: str, max_rework: int) -> tuple[int, bool]:
    """Consecutive failures for an item and whether the rework budget is exhausted."""
    if max_rework < 1:
        raise ValueError(f"max_rework must be >= 1, got {max_rework}.")
    records = iter_evidence(root, item=item)
    records.sort(key=lambda pair: (str(pair[1].get("finished_at", "")), pair[0].name))
    streak = 0
    for _, record in reversed(records):
        kind = record.get("kind")
        verdict = record.get("verdict")
        if kind in ("promotion", "revert"):
            break
        if kind not in ("gate", "adversarial") or verdict == "infra":
            continue
        if verdict == "pass":
            break
        if verdict == "fail":
            streak += 1
    return streak, streak >= max_rework


def write_evidence(root: Path, ev: Evidence) -> Path:
    if ev.schema != SCHEMA_VERSION:
        raise ValueError(f"evidence schema {ev.schema!r} does not match SCHEMA_VERSION {SCHEMA_VERSION}.")
    if ev.verdict not in VERDICTS:
        raise ValueError(f"evidence verdict {ev.verdict!r} is invalid; use one of {VERDICTS}.")
    if not isinstance(ev.commit, str) or not _COMMIT_RE.match(ev.commit):
        raise ValueError(f"evidence commit {ev.commit!r} must be 7-40 lowercase hex characters (a git SHA).")
    if ev.counterexample_id is not None and not _CX_RE.match(ev.counterexample_id):
        raise ValueError(f"counterexample_id {ev.counterexample_id!r} must look like CX-0001.")
    for check in ev.checks:
        if not isinstance(check, CheckResult):
            raise ValueError(f"evidence checks must be CheckResult instances, got {type(check).__name__}.")
    if ev.kind in ("gate", "adversarial") and ev.verdict == "pass" and not all(c.ok for c in ev.checks):
        failing = [c.name for c in ev.checks if not c.ok]
        raise ValueError(f"evidence verdict is 'pass' but checks failed: {failing}. Record verdict 'fail' instead.")
    path = evidence_path(root, ev)
    if path.exists():
        raise FileExistsError(f"{path} already exists; evidence is append-only. Retry to pick the next sequence number.")
    write_json(path, ev)
    return path


def _same_commit(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def promotable(root: Path, stage: int, commit: str) -> bool:
    if len(commit) < 7:
        raise ValueError(f"promotable needs a commit SHA of at least 7 characters, got {commit!r}.")
    gate_pass = False
    adversarial_pass = False
    for _, record in iter_evidence(root, stage=stage):
        if record.get("verdict") != "pass" or not _same_commit(str(record.get("commit", "")), commit):
            continue
        if record.get("kind") == "gate":
            gate_pass = True
        elif record.get("kind") == "adversarial":
            adversarial_pass = True
    return gate_pass and adversarial_pass
