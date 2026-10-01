# @exports: SCHEMA_VERSION: int
# @exports: CheckResult(name:str, cmd:str, exit:int, duration_s:float, ok:bool, stdout_tail:str)
# @exports: Evidence(schema:int, stage:int, item:str, seat:str, kind:str, commit:str, started_at:str, finished_at:str, checks:list[CheckResult], verdict:str, counterexample_id:str|None=None, resources:dict=<{}>, rework_cycle:int=0, budget_exhausted:bool=False, waivers:list[dict]=<[]>, room_ref:str|None=None)
# @exports: Counterexample(schema:int, id:str, stage_found:int, found_at:str, fixed_at:str|None, title:str, invariant:str, runner:str, seed:int, created_by:str, room_ref:str|None)
# @exports: Config(driver_image:str, cpus:float, memory:str, start_timeout_s:int, max_rework:int, build_cache:bool)
# @exports: ConfigError(ValueError)
# @exports: load_config(root: Path|None=None) -> Config
# @exports: repo_root() -> Path
# @exports: tail(text: str, limit: int = 2000) -> str
# @exports: now_iso() -> str
# @exports: write_json(path: Path, obj: dict|list|dataclass) -> None
# @exports: read_json(path: Path) -> dict
# @exports: git(*args: str, cwd: Path|None=None) -> str
# @exports: head_commit(cwd: Path|None=None) -> str
# @exports: snapshot(commit: str, dest: Path, cwd: Path|None=None) -> None
# @imports: none (Python standard library only)
# @env: FACTORY_DRIVER_IMAGE
# @env: FACTORY_CPUS
# @env: FACTORY_MEMORY
# @env: FACTORY_START_TIMEOUT
# @env: FACTORY_MAX_REWORK
# @env: FACTORY_BUILD_CACHE
# @schema: .env file at repo root: KEY=VALUE lines, '#' comments; process environment overrides it
# @schema: write_json output: UTF-8, indent=2, sorted keys, trailing newline, atomic replace
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class CheckResult:
    name: str
    cmd: str
    exit: int
    duration_s: float
    ok: bool
    stdout_tail: str  # at most 2000 characters


@dataclass
class Evidence:
    schema: int
    stage: int
    item: str
    seat: str
    kind: str  # gate|adversarial|review|promotion|revert
    commit: str
    started_at: str
    finished_at: str
    checks: list[CheckResult]
    verdict: str  # pass|fail|infra
    counterexample_id: str | None = None
    resources: dict = field(default_factory=dict)
    rework_cycle: int = 0
    budget_exhausted: bool = False
    waivers: list[dict] = field(default_factory=list)
    room_ref: str | None = None


@dataclass
class Counterexample:
    schema: int
    id: str
    stage_found: int
    found_at: str
    fixed_at: str | None
    title: str
    invariant: str
    runner: str
    seed: int
    created_by: str
    room_ref: str | None


class ConfigError(ValueError):
    """Raised when .env or the process environment holds an invalid FACTORY_* value."""


@dataclass(frozen=True)
class Config:
    driver_image: str
    cpus: float
    memory: str
    start_timeout_s: int
    max_rework: int
    build_cache: bool


def repo_root() -> Path:
    """Repo root, derived from this file's location (factory/lib/common.py)."""
    return Path(__file__).resolve().parents[2]


def tail(text: str, limit: int = 2000) -> str:
    """Last `limit` characters of text."""
    if limit < 1:
        raise ValueError("tail: limit must be >= 1")
    return text if len(text) <= limit else text[-limit:]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE but found {raw!r}. Fix or delete that line.")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


_FACTORY_VARS = (
    "FACTORY_DRIVER_IMAGE",
    "FACTORY_CPUS",
    "FACTORY_MEMORY",
    "FACTORY_START_TIMEOUT",
    "FACTORY_MAX_REWORK",
    "FACTORY_BUILD_CACHE",
)
_MEMORY_RE = re.compile(r"^[1-9][0-9]*[bkmg]$")


def load_config(root: Path | None = None) -> Config:
    """Read and validate every FACTORY_* variable. The only place they are read."""
    base = Path(root).resolve() if root is not None else repo_root()
    values = _load_dotenv(base / ".env")
    for key in _FACTORY_VARS:
        if os.environ.get(key):
            values[key] = os.environ[key]

    def get(key: str, default: str) -> str:
        value = values.get(key, "")
        return value if value != "" else default

    image = get("FACTORY_DRIVER_IMAGE", "python:3.12.7-slim")
    if re.search(r"\s", image):
        raise ConfigError(f"FACTORY_DRIVER_IMAGE={image!r} contains whitespace. Use an image reference like python:3.12.7-slim.")

    raw = get("FACTORY_CPUS", "1")
    try:
        cpus = float(raw)
    except ValueError:
        raise ConfigError(f"FACTORY_CPUS={raw!r} is not a number. Set it to e.g. 1 or 0.5 in .env.") from None
    if not math.isfinite(cpus) or cpus <= 0:
        raise ConfigError(f"FACTORY_CPUS={raw!r} must be a positive number. Set it to e.g. 1 in .env.")

    memory = get("FACTORY_MEMORY", "512m")
    if not _MEMORY_RE.match(memory.lower()):
        raise ConfigError(f"FACTORY_MEMORY={memory!r} is invalid. Use a Docker size such as 512m or 1g.")

    def positive_int(key: str, default: str) -> int:
        text = get(key, default)
        try:
            number = int(text)
        except ValueError:
            raise ConfigError(f"{key}={text!r} is not an integer. Set it to a whole number >= 1 in .env.") from None
        if number < 1:
            raise ConfigError(f"{key}={text!r} must be >= 1.")
        return number

    start_timeout = positive_int("FACTORY_START_TIMEOUT", "30")
    max_rework = positive_int("FACTORY_MAX_REWORK", "3")

    cache = get("FACTORY_BUILD_CACHE", "0")
    if cache not in ("0", "1"):
        raise ConfigError(f"FACTORY_BUILD_CACHE={cache!r} must be 0 or 1.")

    return Config(
        driver_image=image,
        cpus=cpus,
        memory=memory,
        start_timeout_s=start_timeout,
        max_rework=max_rework,
        build_cache=cache == "1",
    )


def write_json(path: Path, obj: object) -> None:
    """Atomic write: indent=2, sorted keys, trailing newline. Dataclasses are converted with asdict."""
    path = Path(path)
    data = asdict(obj) if is_dataclass(obj) and not isinstance(obj, type) else obj
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"JSON file not found: {path}. Run the step that produces it first.") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}). Delete it or regenerate it.") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object but holds a {type(data).__name__}.")
    return data


def git(*args: str, cwd: Path | None = None) -> str:
    cmd = ["git", *args]
    if shutil.which("git") is None:
        raise RuntimeError("git was not found on PATH. Install git and retry.")
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise RuntimeError(
            f"`{' '.join(cmd)}` failed with exit {proc.returncode} in {cwd or Path.cwd()}: {detail}. "
            "Check that you are inside the factory git repo and that the ref exists."
        )
    return proc.stdout.rstrip("\n")


def head_commit(cwd: Path | None = None) -> str:
    return git("rev-parse", "HEAD", cwd=cwd)


def snapshot(commit: str, dest: Path, cwd: Path | None = None) -> None:
    """Export the tree at `commit` into an empty `dest` directory (git archive | tar -x)."""
    for tool in ("git", "tar"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"snapshot needs `{tool}` on PATH. Install it and retry.")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise RuntimeError(f"snapshot destination {dest} is not empty. Pass a fresh directory so the build tree is exactly the commit.")
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", commit], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    extract = subprocess.Popen(["tar", "-x", "-C", str(dest)], stdin=archive.stdout, stderr=subprocess.PIPE)
    assert archive.stdout is not None and archive.stderr is not None
    archive.stdout.close()
    _, tar_err = extract.communicate()
    git_err = archive.stderr.read()
    archive.wait()
    archive.stderr.close()
    if archive.returncode != 0:
        raise RuntimeError(
            f"`git archive {commit}` failed (exit {archive.returncode}): {git_err.decode(errors='replace').strip()}. "
            "Check that the commit exists in this repo."
        )
    if extract.returncode != 0:
        raise RuntimeError(
            f"`tar -x` into {dest} failed (exit {extract.returncode}): {tar_err.decode(errors='replace').strip()}."
        )
