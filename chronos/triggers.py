"""Cross-wedge event bus: the wiring that makes four wedges one product.

Design principle, and it is absolute: TRIGGERS ARE BEST-EFFORT. If a trigger
raises, the originating wedge operation is unaffected. A Wedge 4 block stands
even if Wedge 2's Reflector throws. Every entry point here catches broadly and
logs; nothing propagates. A feedback loop that can break the primary operation
is worse than no feedback loop.

Rationale (why automatic at all): the manual path already exists --
chronos_capture_lesson requires an agent to notice it failed and choose to
report it. Agents that confidently do the wrong thing are exactly the ones that
will not self-report. Automating the highest-signal event (a CI block) closes
the loop without requiring agent cooperation.

Kill switch: CHRONOS_AUTO_TRIGGERS=false disables all three.
"""

import logging
import os
import threading
import weakref
from datetime import datetime, timezone

log = logging.getLogger("chronos.triggers")

# In-flight trigger-1 threads. A WeakSet so finished threads drop out on their
# own and a long-running server does not accumulate references.
_threads = weakref.WeakSet()


def enabled() -> bool:
    """Checked at call time, not import time, so tests and operators can flip it
    without reimporting the package."""
    return os.environ.get("CHRONOS_AUTO_TRIGGERS", "true").strip().lower() not in (
        "false", "0", "no", "off")


# --- Trigger 1: Wedge 4 block -> Wedge 2 Reflector -------------------------
# Rationale: every CI block is a labelled failure -- an agent wrote something,
# and the system rejected it for a stated reason against a known node. That is
# precisely the trace shape the Reflector wants, already assembled. Capturing it
# automatically turns enforcement into training data.

def on_block(rule_id, message, matched_qualified_name=None, agent_id=None,
             session_id=None, rule_text="", driver=None, group_id="default"):
    """A block just happened. Hand it to the Reflector WITHOUT waiting.

    Returns immediately (always None). The enforcement path must not pay for an
    LLM round-trip: validation measured 5,099ms per blocking verdict with this
    inline versus 134ms without it -- a 38x tax on exactly the CI runs that
    block the most. The lesson is still captured, just after the fact.

    Use `run_block_sync(...)` when you need the curator result (tests do)."""
    if not enabled():
        return None
    # Trigger 1: fire-and-forget — enforcer does not block on Reflector result
    t = threading.Thread(
        target=run_block_sync, name=f"chronos-trigger1-{rule_id}", daemon=True,
        kwargs=dict(rule_id=rule_id, message=message,
                    matched_qualified_name=matched_qualified_name,
                    agent_id=agent_id, session_id=session_id,
                    rule_text=rule_text, driver=driver, group_id=group_id))
    t.start()
    _threads.add(t)
    return None


def drain(timeout=30):
    """Wait for in-flight trigger-1 threads. For tests and CLI shutdown, so a
    short-lived process does not exit before the lesson is captured."""
    for t in list(_threads):
        t.join(timeout=timeout)
        if not t.is_alive():
            _threads.discard(t)
    return len(_threads)


def run_block_sync(rule_id, message, matched_qualified_name=None, agent_id=None,
                   session_id=None, rule_text="", driver=None, group_id="default"):
    """The actual reflect->curate work. Runs on the background thread.

    Swallows everything: this is best-effort by contract, and it now runs where
    nothing is left to propagate an exception to."""
    if not enabled():
        return None
    try:
        trace = {
            "agent_id": agent_id or "unknown",
            "session_id": session_id or "ci",
            "action": f"wrote code matching deprecated pattern: {rule_id}",
            "outcome": "rejected",
            "error_or_correction": f"CI enforcement blocked: {message}. Rule: {rule_text}",
            "nodes_touched": [matched_qualified_name] if matched_qualified_name else [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        candidate = _reflect(trace, driver, group_id)
        if candidate is None:
            log.info("auto-trigger: block on %s -> reflector returned None (low-signal)",
                     rule_id)
            return None
        log.info("auto-trigger: block on %s -> reflector produced rule '%s'",
                 rule_id, str(candidate.get("rule_text", ""))[:60])
        # Curator owns the Packmind call; we deliberately do not touch Packmind
        # from the trigger path.
        from . import curator
        return curator.curate(candidate)
    except Exception as e:  # noqa: BLE001 -- best-effort by contract
        log.warning("auto-trigger: block on %s -> trigger failed (%s: %s); "
                    "enforcement result is unaffected", rule_id, type(e).__name__, e)
        return None


def _reflect(trace, driver, group_id):
    """Run the async Reflector from a sync caller.

    Normally called on the trigger's own background thread, where no event loop
    is running, so asyncio.run() is the whole story. The loop-detection branch
    remains for run_block_sync() called directly from inside a loop (tests)."""
    import asyncio

    from . import reflector

    coro = reflector.reflect(driver, group_id, trace)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=120)


# --- Trigger 2: Wedge 1 deprecation -> Wedge 4 coverage check --------------
# Rationale: the most common Chronos failure mode is a node being deprecated in
# the graph with no enforcement rule covering it -- agents keep using it and
# Wedge 4 silently passes, because a rule that does not exist cannot fire. This
# surfaces the gap at the moment of deprecation rather than retroactively.
# Detection only: generating a rule needs an LLM call and human approval, so
# doing it automatically here would be both expensive and presumptuous.

def on_deprecation(qualified_name, valid_at=None, language=None):
    """A node's facts were superseded. Warn if no active rule covers it."""
    if not enabled() or not qualified_name:
        return None
    try:
        from . import rule_store
        rules = rule_store.get_active_rules(language)
        hit = _covering_rule(qualified_name, rules)
        if hit:
            log.info("auto-trigger: node %s deprecated — covered by rule %s",
                     qualified_name, hit["rule_id"])
            return hit["rule_id"]
        log.warning("auto-trigger: node %s deprecated at %s — no enforcement rule "
                    "covers it. Run chronos_generate_rule to create one.",
                    qualified_name, valid_at)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("auto-trigger: coverage check for %s failed (%s: %s)",
                    qualified_name, type(e).__name__, e)
        return None


def _covering_rule(qualified_name, rules):
    """Fuzzy on purpose: this drives a warning, not a gate. A rule covers a node
    if it names it as evidence, or mentions its bare name in the rule text."""
    bare = qualified_name.split("::")[0].split(".")[-1].strip()
    for r in rules or []:
        text = (r.get("rule_text") or "")
        if bare and (bare in text or bare in (r.get("yaml_pattern") or "")):
            return r
        if qualified_name in text:
            return r
    return None


# --- Trigger 3: Wedge 3 conflict -> Wedge 2 lesson opportunity -------------
# Rationale: conflicts are WEAK signal next to CI blocks. Two agents wanting the
# same node is often legitimate concurrency, not a mistake. Auto-reflecting on
# every conflict would flood the playbook with noise and train the Curator's
# dedup to work against us. So this is informational only -- it surfaces the
# opportunity and lets a human or orchestrator decide.

def on_conflict(node_id, agent_id, held_by, intent=None):
    """A lock acquisition lost a race. Log it as a coordination hint."""
    if not enabled():
        return None
    try:
        log.info("auto-trigger: conflict on %s between %s and %s — consider "
                 "capturing a coordination rule via chronos_capture_lesson",
                 node_id, agent_id, held_by)
        return {"node_id": node_id, "agent_id": agent_id, "held_by": held_by}
    except Exception as e:  # noqa: BLE001
        log.warning("auto-trigger: conflict hint failed (%s)", e)
        return None
