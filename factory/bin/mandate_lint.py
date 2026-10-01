# @exports: main(argv: list[str] | None = None) -> int   # exit 0 clean | 1 leaks (or code overlaps with --strict-code) | 2 usage/infra
# @exports: tokens(text: str) -> list[tuple[int, str]]   # (line_no, token) per the PACK tokenizer
# @exports: load_allow(path: Path) -> set[str]
# @imports: factory/lib/common.py:head_commit(), write_json(path, obj)
# @env: none
# @schema: mandate_lint.py [--mandates mandates] --task PATH... [--code DIR...] [--allow factory/generic_vocab.txt] [--out results/mandate_lint.json] [--strict-code]
# @schema: tokens = lowercase letter-words (snake/camel split), len>=4 after plural-'s' strip; plus [45]xx codes and /path-like strings
# @schema: leak = token in mandates INTERSECT (task union other-track text) MINUS allowlist; allowlist words found in task text are reported as allow_hits (warning)
# @schema: out = {"commit","files":[str],"tokens_checked":int,"leaks":[{"file","line","token","source"}],"allow_hits":[str],"code_overlaps":[{"file","line","token","source"}]}
"""Mandate linter: mandates must not share task-specific vocabulary with any track text."""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from factory.lib.common import head_commit, write_json  # noqa: E402

WORD_RE = re.compile(r"[A-Za-z]+")
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
CODE_RE = re.compile(r"(?<!\d)[45]\d\d(?!\d)")
PATH_RE = re.compile(r"(?<![\w/])/[\w\-.{}:]+(?:/[\w\-.{}:]*)*")
CODE_SUFFIXES = {".py", ".html", ".htm", ".js", ".css", ".md", ".json", ".txt", ".sh", ".sql", ".yml", ".yaml"}


def die(msg: str, code: int = 2):
    print(f"mandate_lint: ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def norm_word(word: str) -> str:
    w = word.lower()
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def tokens(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for ln, line in enumerate(text.splitlines(), 1):
        for m in PATH_RE.finditer(line):
            tok = m.group(0).lower().rstrip(".,;:)")
            if len(tok) >= 3:
                out.append((ln, tok))
        for m in CODE_RE.finditer(line):
            out.append((ln, m.group(0)))
        for word in WORD_RE.findall(CAMEL_RE.sub(" ", line)):
            tok = norm_word(word)
            if len(tok) >= 4:
                out.append((ln, tok))
    return out


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        die(f"cannot read {path}: {exc}. Fix the path or save the file as UTF-8.")


def load_allow(path: Path) -> set[str]:
    if not path.is_file():
        die(f"allowlist {path} not found. Create it (one generic word per line) or pass --allow.")
    allow: set[str] = set()
    for raw in read_text(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("/"):
            allow.add(line.lower())
        else:
            allow.update(tok for _ln, tok in tokens(line))
    return allow


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def collect_code_files(dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            die(f"--code directory {d} not found.")
        for p in sorted(d.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in CODE_SUFFIXES:
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(d).parts):
                continue
            files.append(p)
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mandate_lint.py")
    ap.add_argument("--mandates", default="mandates")
    ap.add_argument("--task", nargs="+", required=True)
    ap.add_argument("--code", nargs="+", default=[])
    ap.add_argument("--allow", default="factory/generic_vocab.txt")
    ap.add_argument("--out", default="results/mandate_lint.json")
    ap.add_argument("--strict-code", action="store_true")
    a = ap.parse_args(argv)

    def resolve_path(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else ROOT / q

    mandate_dir = resolve_path(a.mandates)
    if not mandate_dir.is_dir():
        die(f"mandates directory {mandate_dir} not found.")
    mandate_files = sorted(mandate_dir.glob("*.md"))
    if not mandate_files:
        die(f"no *.md mandate files in {mandate_dir}.")
    task_files = [resolve_path(t) for t in a.task]
    for t in task_files:
        if not t.is_file():
            die(f"task file {t} not found. Paste the task text into it first.")
    allow = load_allow(resolve_path(a.allow))

    mand: dict[str, list[tuple[str, int]]] = {}
    for f in mandate_files:
        for ln, tok in tokens(read_text(f)):
            mand.setdefault(tok, []).append((rel(f), ln))

    task_src: dict[str, str] = {}
    for f in task_files:
        for _ln, tok in tokens(read_text(f)):
            task_src.setdefault(tok, rel(f))

    leaks = []
    for tok in sorted(mand):
        if tok in task_src and tok not in allow:
            for file, ln in mand[tok]:
                leaks.append({"file": file, "line": ln, "source": task_src[tok], "token": tok})

    allow_hits = sorted(t for t in allow if t in task_src)

    code_files = collect_code_files([resolve_path(c) for c in a.code])
    code_src: dict[str, str] = {}
    for f in code_files:
        for _ln, tok in tokens(read_text(f)):
            code_src.setdefault(tok, rel(f))
    code_overlaps = []
    for tok in sorted(mand):
        if tok in code_src and tok not in allow:
            for file, ln in mand[tok]:
                code_overlaps.append({"file": file, "line": ln, "source": code_src[tok], "token": tok})

    try:
        commit = head_commit()
    except RuntimeError as exc:
        die(f"cannot read HEAD to cite in results: {exc}. Make at least one commit first.")

    out = resolve_path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        out,
        {
            "allow_hits": allow_hits,
            "code_overlaps": code_overlaps,
            "commit": commit,
            "files": [rel(f) for f in mandate_files + task_files],
            "leaks": leaks,
            "tokens_checked": len(mand),
        },
    )

    for lk in leaks:
        print(f"LEAK {lk['file']}:{lk['line']} '{lk['token']}' also in {lk['source']}")
    for ov in code_overlaps:
        print(f"CODE-OVERLAP {ov['file']}:{ov['line']} '{ov['token']}' also in {ov['source']}")
    for t in allow_hits:
        print(f"WARN allowlist word '{t}' appears in task text; confirm it is truly generic")
    print(
        f"mandate_lint: {len(mand)} tokens checked, {len(leaks)} leaks, "
        f"{len(code_overlaps)} code overlaps, {len(allow_hits)} allow hits -> {rel(out)}"
    )
    if leaks:
        print("FIX: rewrite the flagged mandate lines in generic process language; only add a word to the allowlist if it is truly domain-neutral.")
        return 1
    if code_overlaps and a.strict_code:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
