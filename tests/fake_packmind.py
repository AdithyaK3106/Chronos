"""Fake Packmind — just enough of the real API to exercise chronos/playbook.py
over real sockets, with no Docker and no credentials.

Implements exactly the three endpoints playbook.py calls (all under /api/v0,
all requiring `Authorization: Bearer <anything>`):

  GET  /auth/me                                          -> {organization, spaces}
  GET  /organizations/{org}/spaces/{space}/standards     -> [standard, ...]
  POST /organizations/{org}/spaces/{space}/standards     -> {id}

Plus two admin routes that are NOT in the real API, for tests:

  GET  /admin/rules   -> the raw in-memory list
  POST /admin/reset   -> clear it

What this verifies: URL construction, the bearer header, request/response body
shapes, HTTP error paths, and the evidence block round-tripping through
`description`. What it does NOT verify: whether our reading of Packmind's
TypeScript source is correct. It is a regression test for our client, not
evidence that the real API agrees with it. Only a live run gives you that.

ponytail: in-memory list, no persistence. If a test ever needs restart
survival, that is the moment to add it, not before.
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ORG = "fake-org-001"
SPACE = "fake-space-001"
STANDARDS_PATH = f"/api/v0/organizations/{ORG}/spaces/{SPACE}/standards"

# Module-level so tests and the doctor round-trip can read it without holding
# a server reference.
RULES = []
_lock = threading.Lock()


def reset():
    with _lock:
        RULES.clear()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        """Real Packmind rejects a missing bearer; so do we, or the client's
        auth header could break and no test would notice."""
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            self._send(401, {"error": "missing bearer token"})
            return False
        return True

    def do_GET(self):
        if self.path == "/admin/rules":
            with _lock:
                return self._send(200, list(RULES))
        if not self._authed():
            return
        if self.path == "/api/v0/auth/me":
            return self._send(200, {
                "organization": {"id": ORG},
                "spaces": [{"id": SPACE, "name": "fake space"}],
            })
        if self.path == STANDARDS_PATH:
            with _lock:
                return self._send(200, list(RULES))
        self._send(404, {"error": "not implemented"})

    def do_POST(self):
        if self.path == "/admin/reset":
            reset()
            return self._send(200, {"reset": True})
        if not self._authed():
            return
        if self.path == STANDARDS_PATH:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            with _lock:
                std = {
                    "id": f"fake-rule-{len(RULES) + 1:03d}",
                    "name": body.get("name", ""),
                    "description": body.get("description", ""),
                    "rules": body.get("rules", []),
                    "scope": body.get("scope"),
                    "version": 1,
                    # Real Packmind has no status field ([D1]); a created
                    # standard is simply unpublished. We never expose a publish
                    # route here, mirroring that Chronos never calls one.
                    "published": False,
                }
                RULES.append(std)
            return self._send(201, {"id": std["id"]})
        self._send(404, {"error": "not implemented"})

    def log_message(self, *a):
        pass  # quiet under tests


def serve(port=9999):
    """Start on a background daemon thread. Returns (httpd, actual_port).

    port=0 asks the OS for a free one — use that in tests so a busy 9999
    doesn't cause a spurious failure."""
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="fake Packmind for Chronos tests")
    ap.add_argument("--port", type=int, default=9999)
    args = ap.parse_args()
    httpd, port = serve(args.port)
    print(f"fake packmind on http://127.0.0.1:{port}")
    print(f"  PACKMIND_API_URL=http://127.0.0.1:{port} PACKMIND_API_KEY=fake")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
