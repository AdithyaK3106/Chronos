"""Memory injection precision: memories reach the prompt only when they are about the same thing."""

from benchmarks import memory_eval as m
from chronos.context.session import MEMORY_MIN_RELEVANCE
from chronos.memory.store import memory_terms


def _run(queries):
    saved = m.QUERIES
    m.QUERIES = queries
    try:
        return m.run(recall_kwargs={"min_relevance": MEMORY_MIN_RELEVANCE})
    finally:
        m.QUERIES = saved


def test_unrelated_prompts_get_no_memories():
    for qs in (m.QUERIES, m.HELD_OUT):
        assert _run(qs)["unrelated_prompts_with_injection"] <= 0.05


def test_relevant_memories_still_found():
    assert _run(m.QUERIES)["relevant_hit_rate"] >= 0.6
    assert _run(m.HELD_OUT)["relevant_hit_rate"] >= 0.8


def test_unfiltered_recall_is_what_the_filter_fixes():
    saved = m.QUERIES
    m.QUERIES = m.HELD_OUT
    try:
        assert m.run()["unrelated_prompts_with_injection"] > 0.5
    finally:
        m.QUERIES = saved


def test_memory_terms_stems_and_drops_task_words():
    assert memory_terms("Fix the refunded payments again") == {"refund", "paymen"}


def test_single_memory_store_still_injects_relevant_memory(tmp_path):
    from chronos.core.db import get_connection
    from chronos.memory.store import ChronosMemoryStore

    st = ChronosMemoryStore(con=get_connection(tmp_path / "c.db"))
    st.remember(content="shopkit/api_client.py is generated from spec/api.yaml by tools/gen_client.py; never edit the API client by hand.", repo_namespace="r")
    assert st.recall("Add a timeout to the API client's get_order call", repo_namespace="r", min_relevance=MEMORY_MIN_RELEVANCE)
    assert not st.recall("make the header logo bigger on mobile", repo_namespace="r", min_relevance=MEMORY_MIN_RELEVANCE)
