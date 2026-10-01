# @exports: Resp(status:int, body:bytes, headers:dict[str,str], elapsed_s:float, error:str|None)  # property .json parses body, raises ValueError if not JSON
# @exports: request(method: str, url: str, *, headers: dict|None=None, json_body: object=None, timeout: float=5.0) -> Resp  # never raises; status 0 and error set on failure
# @exports: barrage(jobs: list[dict], *, jitter_ms: float=0.0, seed: int=1) -> list[Resp]  # one Barrier release; results in job order
# @exports: duplicate(job: dict, n: int, *, jitter_ms: float=0.0, seed: int=1) -> list[Resp]
# @exports: abandon(method: str, url: str, *, headers: dict|None=None, json_body: object=None) -> None  # raw socket: send, close without reading
# @exports: base_url() -> str
# @exports: seed() -> int
# @exports: check(name: str, ok: bool, detail: str="") -> bool
# @exports: finish() -> NoReturn
# @imports: none (Python standard library only; no factory modules, so it runs inside the driver container)
# @env: BASE_URL
# @env: SEED
# @schema: job dict keys: method(str), url(str), headers(dict, optional), json_body(any, optional), timeout(float, optional)
# @schema: check() prints one JSON line {"name","ok","detail"}; finish() prints a last line {"violated": bool, "detail": str}
# @schema: exit codes: 0 all checks held, 1 at least one violated, 2 infrastructure error (uncaught exception, no checks recorded)
# @schema: importing this module installs sys.excepthook so any uncaught exception exits 2, never 1
from __future__ import annotations

import http.client
import json as _json
import os
import random
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urlsplit

BARRIER_TIMEOUT_S = 30.0
_JOB_KEYS = {"method", "url", "headers", "json_body", "timeout"}


@dataclass
class Resp:
    status: int
    body: bytes
    headers: dict
    elapsed_s: float
    error: str | None

    @property
    def json(self):
        try:
            return _json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(
                f"response body (HTTP {self.status}) is not valid JSON: {exc}; body starts with {self.body[:80]!r}"
            ) from exc


def _lower_headers(items) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in items}


def request(method: str, url: str, *, headers: dict | None = None, json_body: object = None, timeout: float = 5.0) -> Resp:
    hdrs = dict(headers or {})
    data = None
    if json_body is not None:
        data = _json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        if not any(key.lower() == "content-type" for key in hdrs):
            hdrs["Content-Type"] = "application/json"
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return Resp(response.status, body, _lower_headers(response.headers.items()), time.monotonic() - started, None)
    except urllib.error.HTTPError as exc:
        headers_out = _lower_headers(exc.headers.items()) if exc.headers else {}
        try:
            body = exc.read()
        except OSError as read_exc:
            return Resp(exc.code, b"", headers_out, time.monotonic() - started, f"body read failed: {read_exc}")
        return Resp(exc.code, body, headers_out, time.monotonic() - started, None)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        return Resp(0, b"", {}, time.monotonic() - started, f"{type(exc).__name__}: {exc}")


def _job_kwargs(job: dict) -> dict:
    if not isinstance(job, dict):
        raise ValueError(f"job must be a dict with keys method and url, got {type(job).__name__}.")
    unknown = sorted(set(job) - _JOB_KEYS)
    if unknown:
        raise ValueError(f"job has unknown key(s) {unknown}; allowed keys are {sorted(_JOB_KEYS)}.")
    if "method" not in job or "url" not in job:
        raise ValueError(f"job {job!r} must contain 'method' and 'url'.")
    kwargs = {"method": job["method"], "url": job["url"]}
    if "headers" in job:
        kwargs["headers"] = job["headers"]
    if "json_body" in job:
        kwargs["json_body"] = job["json_body"]
    if "timeout" in job:
        kwargs["timeout"] = job["timeout"]
    return kwargs


def barrage(jobs: list[dict], *, jitter_ms: float = 0.0, seed: int = 1) -> list[Resp]:
    jobs = list(jobs)
    if jitter_ms < 0:
        raise ValueError(f"jitter_ms must be >= 0, got {jitter_ms}.")
    if not jobs:
        return []
    kwargs_list = [_job_kwargs(job) for job in jobs]
    rng = random.Random(seed)
    delays = [rng.uniform(0.0, jitter_ms) / 1000.0 if jitter_ms > 0 else 0.0 for _ in jobs]
    barrier = threading.Barrier(len(jobs))
    results: list[Resp | None] = [None] * len(jobs)
    errors: list[tuple[int, BaseException]] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=BARRIER_TIMEOUT_S)
            if delays[index] > 0:
                time.sleep(delays[index])
            results[index] = request(**kwargs_list[index])
        except BaseException as exc:  # recorded and re-raised below, never dropped
            errors.append((index, exc))
            barrier.abort()

    threads = [threading.Thread(target=worker, args=(i,), name=f"barrage-{i}") for i in range(len(jobs))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        index, exc = errors[0]
        raise RuntimeError(
            f"barrage worker {index} failed: {type(exc).__name__}: {exc}. "
            f"Fewer jobs per barrage may help if the machine cannot start {len(jobs)} threads together."
        )
    return [r for r in results if r is not None]


def duplicate(job: dict, n: int, *, jitter_ms: float = 0.0, seed: int = 1) -> list[Resp]:
    if n < 1:
        raise ValueError(f"duplicate: n must be >= 1, got {n}.")
    return barrage([dict(job) for _ in range(n)], jitter_ms=jitter_ms, seed=seed)


def abandon(method: str, url: str, *, headers: dict | None = None, json_body: object = None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError(f"abandon: url {url!r} must be of the form http://host:port/path.")
    port = parts.port or 80
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    body = b""
    merged: dict[str, str] = {"Host": f"{parts.hostname}:{port}", "Connection": "close"}
    if json_body is not None:
        body = _json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        merged["Content-Type"] = "application/json"
    if body or method.upper() in ("POST", "PUT", "PATCH"):
        merged["Content-Length"] = str(len(body))
    for key, value in (headers or {}).items():
        for existing in [e for e in merged if e.lower() == key.lower()]:
            del merged[existing]
        merged[key] = str(value)
    head = f"{method.upper()} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in merged.items()) + "\r\n"
    try:
        sock = socket.create_connection((parts.hostname, port), timeout=5)
    except OSError as exc:
        raise RuntimeError(
            f"abandon: cannot connect to {parts.hostname}:{port} ({exc}). "
            "Is the service up, and is this script running inside the service's network namespace?"
        ) from exc
    try:
        sock.sendall(head.encode("latin-1") + body)
    finally:
        sock.close()


def base_url() -> str:
    value = os.environ.get("BASE_URL", "")
    if not value:
        raise RuntimeError("BASE_URL is not set. The gate sets it for check scripts; for manual runs use e.g. BASE_URL=http://127.0.0.1:8080.")
    if not value.startswith(("http://", "https://")):
        raise RuntimeError(f"BASE_URL={value!r} must start with http:// or https://.")
    return value.rstrip("/")


def seed() -> int:
    value = os.environ.get("SEED", "1")
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"SEED={value!r} is not an integer. Set SEED to a whole number such as 1.") from None


_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _results.append((name, ok, detail))
    print(_json.dumps({"detail": detail, "name": name, "ok": ok}, sort_keys=True), flush=True)
    return ok


def finish() -> NoReturn:
    if not _results:
        print("INFRA ERROR: finish() was called but no check() was recorded. Call hammer.check(...) at least once.", file=sys.stderr)
        sys.exit(2)
    failed = [(name, detail) for name, ok, detail in _results if not ok]
    if failed:
        detail = "; ".join(f"{name}: {why}" if why else name for name, why in failed)
        print(_json.dumps({"detail": detail, "violated": True}, sort_keys=True), flush=True)
        sys.exit(1)
    print(_json.dumps({"detail": f"{len(_results)} checks held", "violated": False}, sort_keys=True), flush=True)
    sys.exit(0)


def _infra_excepthook(exc_type, exc, tb) -> None:
    traceback.print_exception(exc_type, exc, tb)
    print(
        f"INFRA ERROR: {exc_type.__name__}: {exc}. The check could not run to completion "
        "(exit 2 means infrastructure problem, not an invariant violation).",
        file=sys.stderr,
    )
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(2)


sys.excepthook = _infra_excepthook
