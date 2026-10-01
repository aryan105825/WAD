# @exports: none (script: python3 app.py)
# @imports: none (Python standard library only)
# @env: MODE
# @env: CAPACITY
# @schema: GET /health -> 200 {"ok": true}
# @schema: GET /state -> 200 {"capacity": int, "claimed": int}
# @schema: POST /claim [Idempotency-Key: str] -> 200 {"claim_id": str, "remaining": int} | 409 {"error": "full"}
# @schema: any other path -> 404 {"error": "not found"}; invalid Content-Length -> 400 {"error": "bad request"}
# @schema: MODE=safe uses a lock and an idempotency map; MODE=racy reads, sleeps 2 ms, then writes without a lock and ignores Idempotency-Key
# @schema: listens on 0.0.0.0:8080; MODE and CAPACITY are container-only variables set through service.json "env"
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080
RACY_SLEEP_S = 0.002
MAX_BODY_BYTES = 1_000_000


def load_settings() -> tuple[str, int]:
    """Read and validate MODE and CAPACITY once, at startup."""
    mode = os.environ.get("MODE", "safe")
    if mode not in ("safe", "racy"):
        raise SystemExit(f"MODE={mode!r} is invalid. Set MODE to 'safe' or 'racy'.")
    raw = os.environ.get("CAPACITY", "5")
    try:
        capacity = int(raw)
    except ValueError:
        raise SystemExit(f"CAPACITY={raw!r} is not an integer. Set CAPACITY to a whole number >= 1.") from None
    if capacity < 1:
        raise SystemExit(f"CAPACITY={raw!r} must be >= 1.")
    return mode, capacity


class Store:
    def __init__(self, mode: str, capacity: int) -> None:
        self.mode = mode
        self.capacity = capacity
        self.claimed = 0
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[int, dict]] = {}

    def state(self) -> dict:
        return {"capacity": self.capacity, "claimed": self.claimed}

    def claim(self, key: str | None) -> tuple[int, dict]:
        if self.mode == "racy":
            return self._claim_racy()
        return self._claim_safe(key)

    def _claim_racy(self) -> tuple[int, dict]:
        current = self.claimed
        if current >= self.capacity:
            return 409, {"error": "full"}
        time.sleep(RACY_SLEEP_S)
        self.claimed = current + 1
        return 200, {"claim_id": f"c{current + 1}", "remaining": self.capacity - (current + 1)}

    def _claim_safe(self, key: str | None) -> tuple[int, dict]:
        with self._lock:
            if key is not None and key in self._seen:
                return self._seen[key]
            if self.claimed >= self.capacity:
                return 409, {"error": "full"}
            self.claimed += 1
            response = (200, {"claim_id": f"c{self.claimed}", "remaining": self.capacity - self.claimed})
            if key is not None:
                self._seen[key] = response
            return response


class Handler(BaseHTTPRequestHandler):
    server_version = "selftest/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("%s %s\n" % (self.address_string(), format % args))

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            sys.stderr.write(f"client disconnected before the response was sent: {exc}\n")

    def do_GET(self) -> None:
        store: Store = self.server.store  # type: ignore[attr-defined]
        if self.path == "/health":
            self._send(200, {"ok": True})
        elif self.path == "/state":
            self._send(200, store.state())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        store: Store = self.server.store  # type: ignore[attr-defined]
        if self.path != "/claim":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(400, {"error": "bad request"})
            return
        if length:
            self.rfile.read(length)
        key = (self.headers.get("Idempotency-Key") or "").strip() or None
        status, payload = store.claim(key)
        self._send(status, payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def main() -> None:
    mode, capacity = load_settings()
    server = Server(("0.0.0.0", PORT), Handler)
    server.store = Store(mode, capacity)  # type: ignore[attr-defined]
    sys.stderr.write(f"selftest service listening on :{PORT} mode={mode} capacity={capacity}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
