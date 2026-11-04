"""Memory injection is off by default (memory A/B 2026-11-05); config, env and per-call switches."""

from __future__ import annotations

import json

import pytest

from chronos.context.session import ChronosSessionBootstrap
from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection
from chronos.memory.models import MemoryTier
from chronos.memory.store import ChronosMemoryStore

LESSON = "Invoice totals must round half-up to cents using decimal, never float."


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("CHRONOS_MEMORY_INJECT", raising=False)
    (tmp_path / "billing.py").write_text("def invoice_total(lines):\n    return sum(lines)\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    ChronosMemoryStore(con=con).remember(content=LESSON, repo_namespace=tmp_path.name, tier=MemoryTier.LEARNED,
                                         importance=0.9, tags=["invoice", "rounding"])
    con.close()
    return tmp_path


def _envelope(repo, **kw):
    con = get_connection(repo / ".chronos" / "chronos.db")
    try:
        res = ChronosSessionBootstrap(repo_path=repo, con=con).bootstrap_session(
            task_prompt="Fix invoice total rounding to cents", acquire_leases=False, **kw)
    finally:
        con.close()
    return res


def _text(res):
    return json.dumps(res, default=str)


def test_default_is_off(repo):
    assert ChronosConfig.load(repo).memory.inject is False
    res = _envelope(repo)
    assert LESSON not in _text(res) and res.get("memories_count", 0) == 0


def test_config_turns_it_on(repo):
    (repo / ".chronos" / "config.json").write_text(json.dumps({"memory": {"inject": True}}))
    assert LESSON in _text(_envelope(repo))


def test_env_override_both_ways(repo, monkeypatch):
    monkeypatch.setenv("CHRONOS_MEMORY_INJECT", "1")
    assert ChronosConfig.load(repo).memory.inject is True
    assert LESSON in _text(_envelope(repo))
    (repo / ".chronos" / "config.json").write_text(json.dumps({"memory": {"inject": True}}))
    monkeypatch.setenv("CHRONOS_MEMORY_INJECT", "0")
    assert ChronosConfig.load(repo).memory.inject is False


def test_per_call_argument_wins(repo):
    assert LESSON in _text(_envelope(repo, include_memories=True))
    (repo / ".chronos" / "config.json").write_text(json.dumps({"memory": {"inject": True}}))
    assert LESSON not in _text(_envelope(repo, include_memories=False))


def test_benchmark_harness_keeps_injection_on(monkeypatch):
    monkeypatch.delenv("CHRONOS_MEMORY_INJECT", raising=False)
    import importlib

    import benchmarks.harness.runner as runner

    importlib.reload(runner)
    import os
    assert os.environ.get("CHRONOS_MEMORY_INJECT") == "1"
