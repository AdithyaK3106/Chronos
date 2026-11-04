"""Optional embedding retriever: chunking, incremental SQLite index, search, resolver fusion, config."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from chronos.context.embeddings import EmbeddingIndex, eligible, file_chunks, ollama_embed, rrf
from chronos.context.graph import SQLiteGraphStore
from chronos.context.resolver import EMBED_TOP_SCORE, DeterministicContextResolver
from chronos.core.config import ChronosConfig, EmbeddingsConfig
from chronos.core.db import get_connection

DIM = 64


def fake_embed(texts, timeout):
    """Bag of words hashed into DIM buckets: texts sharing words are similar."""
    out = []
    for t in texts:
        v = [0.0] * DIM
        for w in re.findall(r"[a-z]{3,}", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
        out.append(v)
    from chronos.context.embeddings import _normalise
    return [_normalise(v) for v in out]


FILES = {
    "src/billing/invoice.py": "def compute_invoice_total(lines):\n    # sum invoice lines with tax\n    return sum(l.price for l in lines)\n",
    "src/mail/sender.py": "class Mailer:\n    def send_email(self, to, body):\n        # deliver email over smtp\n        pass\n",
    "src/util/strings.py": "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
    "tests/test_invoice.py": "def test_total():\n    assert True\n",
    "docs/guide.md": "invoice email guide\n",
}


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    get_connection(tmp_path / ".chronos" / "chronos.db").close()
    return tmp_path


@pytest.fixture
def con(repo):
    c = get_connection(repo / ".chronos" / "chronos.db")
    yield c
    c.close()


def _index(con, repo, calls=None):
    def emb(texts, timeout):
        if calls is not None:
            calls.append(len(texts))
        return fake_embed(texts, timeout)
    return EmbeddingIndex(con, repo, model="fake", embed_fn=emb)


def test_eligible_and_chunks():
    assert eligible("src/a.py") and eligible("web/app.tsx") and eligible("pkg/x.go")
    assert not eligible("tests/test_a.py") and not eligible("docs/a.md") and not eligible("node_modules/x.js") and not eligible("a.min.js")
    chunks = file_chunks("src/mail/sender.py", FILES["src/mail/sender.py"].encode())
    names = [n for n, _ in chunks]
    assert names[0] == "" and "Mailer" in names and "Mailer.send_email" in names
    assert all(text.startswith("src/mail/sender.py") for _, text in chunks)


def test_update_is_incremental_and_vectors_are_float32(repo, con):
    calls = []
    idx = _index(con, repo, calls)
    s1 = idx.update()
    assert s1["embedded_files"] == 3 and idx.indexed_files() == 3  # tests/ and docs/ are skipped
    blob = con.execute("SELECT vec FROM embedding_chunks LIMIT 1").fetchone()[0]
    assert len(blob) == DIM * 4

    calls.clear()
    assert idx.update()["embedded_files"] == 0 and calls == []  # nothing changed, nothing embedded

    (repo / "src/util/strings.py").write_text("def slugify(text):\n    return text.strip()\n")
    s3 = idx.update()
    assert s3["embedded_files"] == 1 and s3["unchanged_files"] == 2

    (repo / "src/mail/sender.py").unlink()
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert idx.update()["removed_files"] == 1
    assert idx.indexed_files() == 2
    assert not con.execute("SELECT 1 FROM embedding_chunks WHERE path = 'src/mail/sender.py'").fetchone()


def test_search_ranks_by_meaning(repo, con):
    idx = _index(con, repo)
    idx.update()
    hits = idx.search("the email delivery over smtp is broken")
    assert hits[0].path == "src/mail/sender.py" and hits[0].best_chunk in ("", "Mailer", "Mailer.send_email")
    assert idx.search("invoice total with tax is wrong")[0].path == "src/billing/invoice.py"


def test_search_fails_soft(repo, con):
    idx = _index(con, repo)
    assert idx.search("anything") == []  # empty index
    idx.update()

    def boom(texts, timeout):
        raise OSError("connection refused")
    idx._embed = boom
    assert idx.search("invoice") == []


def _resolver(repo, con, mode="embeddings"):
    idx = _index(con, repo)
    idx.update()
    return DeterministicContextResolver(repo, SQLiteGraphStore(con), embedding_index=idx,
                                        embedding_config=EmbeddingsConfig(enabled=True, mode=mode))


def test_resolver_puts_embedding_files_first(repo, con):
    res = _resolver(repo, con).resolve("customers get the wrong amount on their invoice total with tax")
    assert res.target_files[0] == "src/billing/invoice.py"
    top = res.candidates[0]
    assert top.score == EMBED_TOP_SCORE and any("Embedding similarity" in r for r in top.reasons)


def test_resolver_rrf_mode(repo, con):
    res = _resolver(repo, con, mode="rrf").resolve("email delivery over smtp fails")
    assert res.target_files[0] == "src/mail/sender.py"
    assert any("rank fusion" in r for r in res.candidates[0].reasons)


def test_explicit_paths_skip_embeddings(repo, con):
    res = _resolver(repo, con).resolve("fix the email bug in src/util/strings.py")
    assert not any("Embedding" in r for c in res.candidates for r in c.reasons)


def test_off_by_default_and_session_has_no_index(repo, con):
    cfg = ChronosConfig.load(repo)
    assert cfg.retrieval.embeddings.enabled is False and cfg.retrieval.embeddings.mode == "embeddings"
    from chronos.context.session import ChronosSessionBootstrap

    assert ChronosSessionBootstrap(repo_path=repo, con=con).resolver.embedding_index is None
    (repo / ".chronos" / "config.json").write_text(json.dumps({"retrieval": {"embeddings": {"enabled": True, "model": "m"}}}))
    boot = ChronosSessionBootstrap(repo_path=repo, con=con)
    assert boot.resolver.embedding_index is not None and boot.resolver.embedding_index.model == "m"


def test_rrf():
    assert rrf(["a", "b", "c"], ["b", "a", "d"])[:2] in (["a", "b"], ["b", "a"])
    assert rrf(["x"], ["y", "x"])[0] == "x"


def test_ollama_embed_request_and_normalisation():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.update(body, path=self.path)
            out = json.dumps({"embeddings": [[3.0, 4.0] for _ in body["input"]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        vecs = ollama_embed(f"http://127.0.0.1:{srv.server_port}", "issue-loc-embed", ["a", "b"])
    finally:
        srv.shutdown()
    assert seen["path"] == "/api/embed" and seen["model"] == "issue-loc-embed" and seen["input"] == ["a", "b"]
    assert [round(x, 3) for x in vecs[0]] == [0.6, 0.8] and len(vecs) == 2
