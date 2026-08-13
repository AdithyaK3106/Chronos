"""Curator -> Packmind over a real socket.

tests/test_wedge2.py covers the same path against a fake client object, which
never exercises urllib, the bearer header, URL construction, or JSON encoding.
This does — against tests/fake_packmind.py on localhost. Still no Docker, no
credentials, no outbound network.

Run: python tests/test_curator_http.py
"""

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_packmind  # noqa: E402
from chronos import curator  # noqa: E402
from chronos.playbook import EVIDENCE_MARK, Packmind  # noqa: E402

CANDIDATE = {
    "rule_text": "IF a function makes an HTTP request THEN it must call the shared "
                 "http client wrapper instead of invoking fetch() directly.",
    "confidence": 0.95,
    "evidence_node": "src/api/actors.ts::getActor",
    "evidence_valid_at": "2026-07-20T15:50:43+00:00",
    "evidence_commit_context": "33 facts, 0 superseded; callers=[create, update]",
    "source_trace_id": "trace-http-001",
    "agent_id": "claude-code",
    "captured_at": "2026-08-14T09:00:00+00:00",
}


def stub_llm(passes=True):
    """Quality gate is an LLM call; this test is about HTTP, not judgement."""
    curator.complete = lambda prompt, model=None: (
        '{"passes_gate": %s, "reason": "stubbed"}' % ("true" if passes else "false")
    )


def stub_embed():
    """Orthogonal vectors -> cosine 0, so dedup never fires. The real dedup
    logic is covered in test_wedge2.py."""
    curator.embed = lambda texts, model=None: [
        [1.0 if i == n else 0.0 for i in range(len(texts))] for n in range(len(texts))
    ]


def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        import json
        return json.loads(r.read().decode())


def main():
    httpd, port = fake_packmind.serve(port=0)  # port 0: never collides
    base = f"http://127.0.0.1:{port}"
    os.environ["PACKMIND_API_URL"] = base
    os.environ["PACKMIND_API_KEY"] = "fake"
    os.environ.pop("PACKMIND_ORG_ID", None)    # force /auth/me discovery
    os.environ.pop("PACKMIND_SPACE_ID", None)
    checks = 0
    try:
        stub_llm(passes=True)
        stub_embed()

        # Clean slate, and prove /admin/reset works.
        urllib.request.urlopen(
            urllib.request.Request(f"{base}/admin/reset", method="POST"), timeout=5
        )
        assert get_json(f"{base}/admin/rules") == [], "reset should empty the store"
        checks += 1

        # The real thing: Packmind() reads env, resolves org/space via
        # /auth/me, lists (empty), then POSTs. All over the socket.
        result = curator.curate(CANDIDATE, packmind=Packmind())
        assert result["submitted"] is True, result
        assert result["packmind_proposal_id"] == "fake-rule-001", result
        checks += 1

        # 4. The rule actually arrived.
        stored = get_json(f"{base}/admin/rules")
        assert len(stored) == 1, stored
        std = stored[0]
        assert CANDIDATE["rule_text"] in std["description"]
        assert std["rules"][0]["content"] == CANDIDATE["rule_text"]
        assert std["published"] is False, "Chronos must never publish — that is the gate"
        checks += 1

        # Evidence survives the description round-trip (the bent-schema hack).
        assert EVIDENCE_MARK in std["description"]
        back = Packmind().list_rules()
        assert len(back) == 1, back
        assert back[0]["evidence_node"] == CANDIDATE["evidence_node"]
        assert back[0]["evidence_valid_at"] == CANDIDATE["evidence_valid_at"]
        assert back[0]["status"] == "proposed"
        assert back[0]["rule_text"].startswith("IF a function makes an HTTP request")
        checks += 1

        # A rejected gate must not write anything.
        stub_llm(passes=False)
        r2 = curator.curate(CANDIDATE, packmind=Packmind())
        assert r2["submitted"] is False, r2
        assert len(get_json(f"{base}/admin/rules")) == 1, "gate reject must not POST"
        checks += 1

        # Unconfigured fails at construction, before any socket is opened.
        # Must clear the env too — the constructor falls back to it by design.
        from chronos.playbook import PackmindError
        saved = os.environ.pop("PACKMIND_API_KEY")
        try:
            Packmind(url=base)
            raise AssertionError("missing key should have raised")
        except PackmindError as e:
            assert "must be set" in str(e), e
        finally:
            os.environ["PACKMIND_API_KEY"] = saved
        checks += 1

        # A present-but-wrong key must fail at the server (401), proving the
        # bearer header is actually sent and actually checked.
        try:
            Packmind(url=base, key="fake", org=fake_packmind.ORG,
                     space=fake_packmind.SPACE)._call("GET", "/auth/me")
        except PackmindError as e:
            raise AssertionError(f"valid key should have worked: {e}")
        req = urllib.request.Request(f"{base}/api/v0/auth/me")  # no auth header
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("missing bearer should have 401'd")
        except urllib.error.HTTPError as e:
            assert e.code == 401, e.code
        checks += 1

        # An unimplemented route 404s as PackmindError, not a crash.
        try:
            Packmind(url=base, key="fake")._call("GET", "/nope")
            raise AssertionError("unknown route should have raised")
        except PackmindError as e:
            assert "404" in str(e), e
        checks += 1

        print(f"ALL PASS ({checks} checks, real HTTP, no Docker)")
        return 0
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
