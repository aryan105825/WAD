# @exports: ServiceSpec(dockerfile:str, port:int, health_path:str, start_timeout_s:int, cpus:float, memory:str, env:dict, build_network:str, waivers:list)
# @exports: load_spec(stage_dir: Path, env_overrides: dict[str,str]|None=None) -> ServiceSpec
# @exports: build(tree: Path, spec: ServiceSpec, tag: str, cache: bool=False) -> CheckResult
# @exports: start(tag: str, name: str, spec: ServiceSpec) -> CheckResult
# @exports: wait_healthy(name: str, spec: ServiceSpec) -> CheckResult
# @exports: run_driver(name: str, repo_snapshot: Path, argv: list[str], env: dict[str,str], timeout_s: int) -> CheckResult
# @exports: probe_no_egress(name: str) -> CheckResult
# @exports: stop(name: str) -> None
# @exports: Sandbox(tree: Path, spec: ServiceSpec, label: str, cache: bool=False)  # context manager; attrs: base_url:str, results:list[CheckResult], ok:bool, name:str, tag:str; method run_check(repo_snapshot: Path, argv: list[str], env: dict[str,str]|None=None, timeout_s: int=120) -> CheckResult
# @imports: factory/lib/common.py:CheckResult, load_config, read_json, tail
# @env: none (driver image and defaults come from common.load_config)
# @schema: service.json keys: dockerfile, port, health_path, start_timeout_s, cpus, memory, env, build_network(none|default), waivers[{id,reason}]; unknown keys are rejected
# @schema: build context is `tree` (the stage directory inside a git-archive snapshot); the Dockerfile path is relative to it
# @schema: service runs `--network none`; drivers join its namespace with `--network container:<name>` and mount the repo snapshot read-only at /repo (cwd /repo, PYTHONPATH=/repo)
# @schema: Sandbox.__enter__ runs build -> start -> wait_healthy -> probe_no_egress, stops at the first failing step (ok=False), never raises for a failed step
# @schema: container name factory-<label>-<pid>; image tag factory-<label>:<pid>; both removed on exit
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from factory.lib.common import CheckResult, load_config, read_json, tail

BUILD_TIMEOUT_S = 900
DOCKER_RUN_TIMEOUT_S = 120
DRIVER_SLACK_S = 60
PROBE_TIMEOUT_S = 90

_SPEC_KEYS = {"dockerfile", "port", "health_path", "start_timeout_s", "cpus", "memory", "env", "build_network", "waivers"}
_MEMORY_RE = re.compile(r"^[1-9][0-9]*[bkmg]$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")

_HEALTH_SCRIPT = """
import sys, time, urllib.request, urllib.error
url, timeout = sys.argv[1], float(sys.argv[2])
deadline = time.monotonic() + timeout
last = "no attempt made"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                print("healthy: HTTP 200 from " + url)
                sys.exit(0)
            last = "HTTP %d" % response.status
    except urllib.error.HTTPError as exc:
        last = "HTTP %d" % exc.code
    except Exception as exc:
        last = "%s: %s" % (type(exc).__name__, exc)
    time.sleep(0.25)
print("not healthy after %ss; last result: %s" % (timeout, last))
sys.exit(1)
"""

_EGRESS_SCRIPT = """
import socket, sys
leaks = []
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    leaks.append("TCP 1.1.1.1:443 connected")
except OSError as exc:
    print("tcp blocked: %s" % exc)
try:
    socket.getaddrinfo("example.com", 443)
    leaks.append("DNS lookup of example.com succeeded")
except OSError as exc:
    print("dns blocked: %s" % exc)
if leaks:
    print("EGRESS DETECTED: " + "; ".join(leaks))
    sys.exit(1)
print("NO_EGRESS_CONFIRMED")
sys.exit(0)
"""


@dataclass
class ServiceSpec:
    dockerfile: str
    port: int
    health_path: str
    start_timeout_s: int
    cpus: float
    memory: str
    env: dict
    build_network: str
    waivers: list


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_spec(stage_dir: Path, env_overrides: dict[str, str] | None = None) -> ServiceSpec:
    cfg = load_config()
    path = Path(stage_dir) / "service.json"
    raw = read_json(path)

    def bad(key: str, message: str) -> ValueError:
        return ValueError(f"{path}: '{key}' {message}. Fix service.json.")

    unknown = sorted(set(raw) - _SPEC_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {unknown}; allowed keys are {sorted(_SPEC_KEYS)}. Fix the typo.")

    dockerfile = raw.get("dockerfile", "Dockerfile")
    if not isinstance(dockerfile, str) or not dockerfile:
        raise bad("dockerfile", "must be a non-empty string")
    if Path(dockerfile).is_absolute() or ".." in Path(dockerfile).parts:
        raise bad("dockerfile", "must be a relative path inside the stage directory")

    port = raw.get("port", 8080)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise bad("port", "must be an integer from 1 to 65535")

    health_path = raw.get("health_path", "/health")
    if not isinstance(health_path, str) or not health_path.startswith("/"):
        raise bad("health_path", "must be a string starting with '/'")

    start_timeout = raw.get("start_timeout_s", cfg.start_timeout_s)
    if not isinstance(start_timeout, int) or isinstance(start_timeout, bool) or start_timeout < 1:
        raise bad("start_timeout_s", "must be an integer >= 1")

    cpus = raw.get("cpus", cfg.cpus)
    if not _is_number(cpus) or cpus <= 0:
        raise bad("cpus", "must be a positive number")

    memory = raw.get("memory", cfg.memory)
    if not isinstance(memory, str) or not _MEMORY_RE.match(memory.lower()):
        raise bad("memory", "must be a Docker size such as 512m or 1g")

    env = raw.get("env", {})
    if not isinstance(env, dict):
        raise bad("env", "must be an object of string values")
    merged_env: dict[str, str] = {}
    for key, value in env.items():
        if not _ENV_KEY_RE.match(key) or not isinstance(value, str):
            raise bad("env", f"entry {key!r} must have a valid name and a string value")
        merged_env[key] = value
    for key, value in (env_overrides or {}).items():
        if not _ENV_KEY_RE.match(key) or not isinstance(value, str):
            raise ValueError(f"env override {key!r}={value!r} must have a valid name and a string value.")
        merged_env[key] = value

    build_network = raw.get("build_network", "default")
    if build_network not in ("none", "default"):
        raise bad("build_network", "must be 'none' or 'default'")

    waivers = raw.get("waivers", [])
    if not isinstance(waivers, list):
        raise bad("waivers", "must be a list")
    for waiver in waivers:
        if (
            not isinstance(waiver, dict)
            or not isinstance(waiver.get("id"), str)
            or not waiver["id"]
            or not isinstance(waiver.get("reason"), str)
            or not waiver["reason"]
        ):
            raise bad("waivers", "entries must be objects with non-empty string 'id' and 'reason'")

    return ServiceSpec(
        dockerfile=dockerfile,
        port=port,
        health_path=health_path,
        start_timeout_s=start_timeout,
        cpus=float(cpus),
        memory=memory,
        env=merged_env,
        build_network=build_network,
        waivers=[dict(w) for w in waivers],
    )


def _to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _exec(cmd: list[str], name: str, timeout_s: int) -> CheckResult:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=timeout_s
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"`{cmd[0]}` was not found on PATH ({exc}). Install Docker >= 24 and make sure the docker CLI works."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        output = _to_text(exc.stdout) + f"\n[timed out after {timeout_s}s: fix the hang or raise the limit]"
        return CheckResult(name, shlex.join(cmd), 124, round(time.monotonic() - started, 3), False, tail(output))
    return CheckResult(
        name,
        shlex.join(cmd),
        proc.returncode,
        round(time.monotonic() - started, 3),
        proc.returncode == 0,
        tail(proc.stdout),
    )


def build(tree: Path, spec: ServiceSpec, tag: str, cache: bool = False) -> CheckResult:
    tree = Path(tree).resolve()
    dockerfile = tree / spec.dockerfile
    if not dockerfile.is_file():
        return CheckResult(
            "build", f"docker build -f {dockerfile}", 2, 0.0, False,
            f"Dockerfile not found at {dockerfile}. Commit it in the stage directory and set 'dockerfile' in service.json.",
        )
    cmd = ["docker", "build", "-t", tag, "-f", str(dockerfile)]
    if not cache:
        cmd.append("--no-cache")
    if spec.build_network == "none":
        cmd += ["--network", "none"]
    cmd.append(str(tree))
    return _exec(cmd, "build", BUILD_TIMEOUT_S)


def start(tag: str, name: str, spec: ServiceSpec) -> CheckResult:
    cmd = ["docker", "run", "-d", "--name", name, "--network", "none", "--cpus", f"{spec.cpus:g}", "--memory", spec.memory]
    for key in sorted(spec.env):
        cmd += ["-e", f"{key}={spec.env[key]}"]
    cmd.append(tag)
    return _exec(cmd, "start", DOCKER_RUN_TIMEOUT_S)


def _inline_driver(name: str, script: str, args: list[str], timeout_s: int, label: str) -> CheckResult:
    cfg = load_config()
    cmd = ["docker", "run", "--rm", "--network", f"container:{name}", cfg.driver_image, "python3", "-c", script, *args]
    return _exec(cmd, label, timeout_s)


def wait_healthy(name: str, spec: ServiceSpec) -> CheckResult:
    url = f"http://127.0.0.1:{spec.port}{spec.health_path}"
    result = _inline_driver(
        name, _HEALTH_SCRIPT, [url, str(spec.start_timeout_s)], spec.start_timeout_s + DRIVER_SLACK_S, "healthy"
    )
    if result.ok:
        return result
    state = _exec(["docker", "inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}}", name], "inspect", 30)
    logs = _exec(["docker", "logs", "--tail", "40", name], "logs", 30)
    detail = (
        result.stdout_tail[-600:]
        + f"\n--- container state: {state.stdout_tail.strip()} ---\n"
        + tail(logs.stdout_tail, 1200)
    )
    return replace(result, stdout_tail=tail(detail))


def run_driver(name: str, repo_snapshot: Path, argv: list[str], env: dict[str, str], timeout_s: int) -> CheckResult:
    if not argv:
        raise ValueError("run_driver: argv must not be empty; pass e.g. ['stage-1/acceptance/check_x.py'].")
    snap = Path(repo_snapshot).resolve()
    if not snap.is_dir():
        raise FileNotFoundError(f"run_driver: repo snapshot {snap} is not a directory. Create it with common.snapshot().")
    cfg = load_config()
    merged = {"PYTHONPATH": "/repo", "PYTHONDONTWRITEBYTECODE": "1"}
    merged.update(env)
    cmd = ["docker", "run", "--rm", "--network", f"container:{name}", "-v", f"{snap}:/repo:ro", "-w", "/repo"]
    for key in sorted(merged):
        cmd += ["-e", f"{key}={merged[key]}"]
    cmd += [cfg.driver_image, "python3", *argv]
    label = "driver-inline" if argv[0] == "-c" else Path(argv[0]).name
    return _exec(cmd, label, timeout_s)


def probe_no_egress(name: str) -> CheckResult:
    result = _inline_driver(name, _EGRESS_SCRIPT, [], PROBE_TIMEOUT_S, "egress_probe")
    confirmed = result.exit == 0 and "NO_EGRESS_CONFIRMED" in result.stdout_tail
    return replace(result, ok=confirmed)


def stop(name: str) -> None:
    result = _exec(["docker", "rm", "-f", name], "stop", 60)
    if not result.ok and "No such container" not in result.stdout_tail:
        raise RuntimeError(f"could not remove container {name}: {result.stdout_tail.strip()}. Run `docker rm -f {name}` manually.")


def _remove_image(tag: str) -> None:
    result = _exec(["docker", "rmi", "-f", tag], "rmi", 120)
    if not result.ok and "No such image" not in result.stdout_tail:
        raise RuntimeError(f"could not remove image {tag}: {result.stdout_tail.strip()}. Run `docker rmi -f {tag}` manually.")


class Sandbox:
    def __init__(self, tree: Path, spec: ServiceSpec, label: str, cache: bool = False):
        if not _LABEL_RE.match(label):
            raise ValueError(f"Sandbox label {label!r} is invalid: use letters, digits, '_', '.', '-' (max 41 chars).")
        self.tree = Path(tree).resolve()
        self.spec = spec
        self.label = label
        self.cache = cache
        suffix = str(os.getpid())
        self.tag = f"factory-{label.lower()}:{suffix}"
        self.name = f"factory-{label.lower()}-{suffix}"
        self.base_url = f"http://127.0.0.1:{spec.port}"
        self.results: list[CheckResult] = []
        self.ok = False

    def __enter__(self) -> "Sandbox":
        steps = (
            lambda: build(self.tree, self.spec, self.tag, self.cache),
            lambda: start(self.tag, self.name, self.spec),
            lambda: wait_healthy(self.name, self.spec),
            lambda: probe_no_egress(self.name),
        )
        self.ok = True
        try:
            for step in steps:
                result = step()
                self.results.append(result)
                if not result.ok:
                    self.ok = False
                    break
        except BaseException:
            self._cleanup()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._cleanup()
        return False

    def _cleanup(self) -> None:
        stop(self.name)
        _remove_image(self.tag)

    def run_check(
        self, repo_snapshot: Path, argv: list[str], env: dict[str, str] | None = None, timeout_s: int = 120
    ) -> CheckResult:
        if not self.ok:
            raise RuntimeError(
                f"Sandbox {self.name} is not healthy, so checks cannot run. Inspect Sandbox.results for the failing step."
            )
        merged = {"BASE_URL": self.base_url}
        merged.update(env or {})
        result = run_driver(self.name, repo_snapshot, argv, merged, timeout_s)
        self.results.append(result)
        return result
