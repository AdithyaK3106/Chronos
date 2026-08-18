"""Self-check for the intent/provenance ledger (Wedge 3).

Run: python tests/test_wedge3.py
"""

import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from chronos import ledger

NODE = "src/api.ts::createClient::Function"
OTHER = "src/db.ts::connect::Function"


def main():
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "ledger.db"
    os.environ["CHRONOS_LEDGER"] = str(db)
    con = ledger.connect(db)

    # --- schema ---
    s = ledger.status(con)
    assert s["tables_ok"] and s["active_locks"] == 0 and s["events"] == 0, s
    print("ok  schema created, ledger empty")

    # --- two agents, same node: second gets the conflict with who/why ---
    a = ledger.acquire(con, NODE, "agent-a", "sess-1", "refactor to async", 300)
    assert a["acquired"] and not a["renewed"], a
    b = ledger.acquire(con, NODE, "agent-b", "sess-2", "add retry logic", 300)
    assert b["acquired"] is False and b["reason"] == "conflict", b
    assert b["conflict"]["agent_id"] == "agent-a", b
    assert b["conflict"]["intent"] == "refactor to async", b
    print(f"ok  conflict reports holder + intent: {b['conflict']['agent_id']} / "
          f"{b['conflict']['intent']!r}")

    # --- same agent re-acquiring extends rather than failing ---
    again = ledger.acquire(con, NODE, "agent-a", "sess-1", "refactor to async", 600)
    assert again["acquired"] and again["renewed"], again
    assert ledger.status(con)["active_locks"] == 1, "renew must not create a second lock"
    print("ok  re-acquire by holder extends the lock")

    # --- release by the wrong agent fails; by the owner succeeds ---
    bad = ledger.release(con, NODE, "agent-b", "sess-2")
    assert bad["released"] is False and bad["reason"] == "not_owner", bad
    assert bad["held_by"]["agent_id"] == "agent-a", bad
    good = ledger.release(con, NODE, "agent-a", "sess-1")
    assert good["released"], good
    assert ledger.release(con, NODE, "agent-a")["reason"] == "not_locked"
    print("ok  release rejects non-owner, succeeds for owner")

    # --- release returns the lock's intent, for wedge3_mcp's propose-rule nudge ---
    ledger.acquire(con, NODE, "agent-a", "sess-1", "refactor the fallback loop", 300)
    released = ledger.release(con, NODE, "agent-a", "sess-1")
    assert released["intent"] == "refactor the fallback loop", released
    assert "intent" not in ledger.release(con, NODE, "agent-a")  # not_locked: nothing to report
    print("ok  release carries the lock's intent back for the caller")

    # --- expiry: an expired lock is swept, not treated as held ---
    ledger.acquire(con, NODE, "agent-a", "sess-1", "short lived", 1)
    assert ledger.status(con)["active_locks"] == 1
    con.execute("UPDATE intent_locks SET expires_at = '2020-01-01T00:00:00+00:00' "
                "WHERE node_id = ?", (NODE,))  # simulate TTL passing
    taken = ledger.acquire(con, NODE, "agent-b", "sess-2", "takes over", 300)
    assert taken["acquired"], f"expired lock must not block: {taken}"
    assert ledger.status(con)["active_locks"] == 1, "sweep must not leave a stale row"
    ledger.release(con, NODE, "agent-b")
    print("ok  expired lock swept on next acquire")

    # --- real TTL, not just a hand-edited row ---
    ledger.acquire(con, OTHER, "agent-a", "s", "brief", 1)
    time.sleep(1.1)
    assert ledger.acquire(con, OTHER, "agent-b", "s", "after ttl", 60)["acquired"], \
        "lock should expire after its ttl elapses"
    ledger.release(con, OTHER, "agent-b")
    print("ok  ttl actually elapses (1s wait)")

    # --- check_conflicts across a set ---
    ledger.acquire(con, NODE, "agent-a", "sess-1", "editing", 300)
    c = ledger.check_conflicts(con, [NODE, OTHER, "src/x.ts::f::Function", NODE])
    assert c["checked"] == 3, c  # deduped
    assert c["conflict_count"] == 1 and c["locked"][0]["node_id"] == NODE, c
    assert set(c["free"]) == {OTHER, "src/x.ts::f::Function"}, c
    print(f"ok  check_conflicts: {c['conflict_count']} locked, {len(c['free'])} free")

    # --- provenance append + retrieval, newest first ---
    ledger.log_event(con, NODE, "agent-a", "sess-1", "modified", "switched to async")
    ledger.log_event(con, NODE, "agent-b", "sess-2", "reviewed", "looks correct")
    h = ledger.history(con, NODE)
    assert h["count"] == 2, h
    assert h["events"][0]["action"] == "reviewed", h["events"]  # newest first
    assert h["events"][1]["reason"] == "switched to async", h["events"]
    assert ledger.history(con, "never/touched::x::Function")["count"] == 0
    print(f"ok  provenance: {h['count']} events, newest first")

    # --- append-only: logging never mutates prior rows ---
    first_id = h["events"][1]["id"]
    ledger.log_event(con, NODE, "agent-c", "s3", "modified", "third")
    rows = ledger.history(con, NODE, limit=99)["events"]
    assert rows[-1]["id"] == first_id and rows[-1]["reason"] == "switched to async", rows[-1]
    assert len(rows) == 3, rows
    print("ok  append-only: earlier events unchanged")

    # --- limit is honoured ---
    assert ledger.history(con, NODE, limit=2)["count"] == 2
    print("ok  history limit honoured")

    # --- concurrency: N threads race for one node, exactly one wins ---
    con.close()
    race_node = "src/race.ts::hot::Function"
    results = []
    lock = threading.Lock()

    def worker(i):
        c = ledger.connect(db)
        try:
            r = ledger.acquire(c, race_node, f"agent-{i}", f"s{i}", "race", 300)
            with lock:
                results.append(r["acquired"])
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(results) == 1, f"exactly one agent must win, got {sum(results)} of {len(results)}"
    print(f"ok  12 threads raced, exactly 1 acquired")

    # --- ledger is independent of the Wedge 1 graph store ---
    con = ledger.connect(db)
    assert "intent_locks" in {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    st = ledger.status(con)
    assert st["active_locks"] == 2 and st["events"] == 3, st
    con.close()
    print(f"ok  doctor status: {st['active_locks']} locks, {st['events']} events")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
