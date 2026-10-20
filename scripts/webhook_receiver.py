#!/usr/bin/env python3
"""Minimal webhook sink for the Quac local end-to-end environment.

Accepts the signed callback the worker POSTs after each simulation, verifies
the HMAC-SHA256 ``X-Quac-Signature`` header against ``QUAC_WEBHOOK_SECRET``
(same secret the worker signs with), and prints a structured line per delivery.

Endpoints:
    POST /hook   — receives worker callbacks (200 on valid signature)
    GET  /hooks  — JSON dump of everything received so far (for assertions)
    GET  /health — liveness probe
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("QUAC_WEBHOOK_PORT", "9099"))
SECRET = os.environ.get("QUAC_WEBHOOK_SECRET", "local-dev-secret").encode()
MAX_BODY = 1 * 1024 * 1024

_RECEIVED: list[dict] = []


class HookHandler(BaseHTTPRequestHandler):
    server_version = "QuacWebhookSink/1.0"

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, {"status": "ok", "received": len(_RECEIVED)})
        elif self.path == "/hooks":
            self._respond(200, {"received": len(_RECEIVED), "hooks": _RECEIVED})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/hook":
            self._respond(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > MAX_BODY:
            self._respond(413, {"error": "bad content length"})
            return
        raw = self.rfile.read(length)

        signature = self.headers.get("X-Quac-Signature", "")
        expected = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        # Worker may prefix with "sha256=" — accept both spellings.
        provided = signature.removeprefix("sha256=")
        if not hmac.compare_digest(provided, expected):
            print(f"[webhook] REJECTED bad signature: {signature!r}", flush=True)
            self._respond(401, {"error": "invalid signature"})
            return

        try:
            event = json.loads(raw)
        except ValueError:
            self._respond(400, {"error": "invalid json"})
            return

        entry = {
            "job_id": event.get("job_id"),
            "status": event.get("status"),
            "attempt": event.get("attempt"),
            "result_key": event.get("result_key"),
        }
        _RECEIVED.append(entry)
        print(
            "[webhook] received "
            f"job={entry['job_id']} status={entry['status']} "
            f"attempt={entry['attempt']} result={entry['result_key']}",
            flush=True,
        )
        self._respond(200, {"ok": True})

    def log_message(self, fmt: str, *args) -> None:  # silence default access log
        pass


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HookHandler)
    print(f"[webhook] sink listening on :{PORT}/hook", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
