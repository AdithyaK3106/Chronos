"""Tests for sub-file AST node locking and intent ledger."""

import threading
import time
from chronos.coordination.ledger import (
    acquire_lock,
    release_lock,
    check_conflicts,
    list_active_locks,
    get_provenance,
)

NODE_A = "src/api.py::create_user::Function"
NODE_B = "src/db.py::get_connection::Function"

def test_atomic_acquire_and_conflict(memory_db):
    # Agent 1 acquires NODE_A
    res1 = acquire_lock(memory_db, NODE_A, "agent_1", "sess_1", "refactor to async", ttl=300)
    assert res1["acquired"] is True
    assert res1["renewed"] is False

    # Agent 2 attempts to acquire same node -> conflict
    res2 = acquire_lock(memory_db, NODE_A, "agent_2", "sess_2", "add validation", ttl=300)
    assert res2["acquired"] is False
    assert res2["reason"] == "conflict"
    assert res2["conflict"]["agent_id"] == "agent_1"
    assert res2["conflict"]["intent"] == "refactor to async"

def test_lease_renewal(memory_db):
    acquire_lock(memory_db, NODE_A, "agent_1", "sess_1", "refactor", ttl=300)
    
    # Same agent re-acquiring extends the lock
    renew = acquire_lock(memory_db, NODE_A, "agent_1", "sess_1", "refactor step 2", ttl=600)
    assert renew["acquired"] is True
    assert renew["renewed"] is True

    # Exactly 1 lock exists
    active = list_active_locks(memory_db)
    assert len(active) == 1
    assert active[0]["intent"] == "refactor step 2"

def test_release_authorization(memory_db):
    acquire_lock(memory_db, NODE_A, "agent_1", "sess_1", "refactor")

    # Wrong agent tries to release -> rejected
    bad_rel = release_lock(memory_db, NODE_A, "agent_2", "sess_2")
    assert bad_rel["released"] is False
    assert bad_rel["reason"] == "not_owner"

    # Owner releases -> succeeds
    good_rel = release_lock(memory_db, NODE_A, "agent_1", "sess_1")
    assert good_rel["released"] is True

    # Now unlocked
    conflicts = check_conflicts(memory_db, [NODE_A])
    assert NODE_A in conflicts["free"]

def test_lock_ttl_expiry(memory_db):
    # Acquire with 1s TTL
    acquire_lock(memory_db, NODE_A, "agent_1", "sess_1", "quick task", ttl=1)
    time.sleep(1.2)

    # Next acquire by agent_2 should succeed because lock expired
    res = acquire_lock(memory_db, NODE_A, "agent_2", "sess_2", "takeover")
    assert res["acquired"] is True
    assert res["agent_id"] == "agent_2"

def test_concurrent_lock_race(tmp_path):
    from chronos.core.db import get_connection
    db_file = tmp_path / "race.db"
    # Initialize schema
    init_con = get_connection(db_file)
    init_con.close()

    winners = []
    def try_acquire(agent_idx):
        con = get_connection(db_file)
        try:
            res = acquire_lock(con, NODE_B, f"agent_{agent_idx}", f"sess_{agent_idx}", "race")
            if res.get("acquired"):
                winners.append(agent_idx)
        finally:
            con.close()

    threads = [threading.Thread(target=try_acquire, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one winner out of 10 racing threads
    assert len(winners) == 1
