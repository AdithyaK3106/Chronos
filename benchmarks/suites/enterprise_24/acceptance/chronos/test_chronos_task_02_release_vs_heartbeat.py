"""chronos_task_02: a heartbeat racing a release must never resurrect the released lock."""
import threading

import pytest

from chronos.coordination.ledger import acquire_lock, check_conflicts, release_lock, renew_lock
from chronos.core.db import get_connection

NODE = "svc.py::charge::Function"


@pytest.fixture
def con(tmp_path):
    c = get_connection(tmp_path / "ledger.db")
    yield c
    c.close()


def held(con):
    return NODE in check_conflicts(con, [NODE]).get("locked", {})


def test_heartbeat_after_release_does_not_recreate_the_lock(con):
    acquire_lock(con, NODE, "agent-a", "s1", "fix charge")
    assert release_lock(con, NODE, "agent-a", "s1")["released"]
    res = renew_lock(con, NODE, "agent-a", "s1")
    assert res["renewed"] is False and res["reason"] == "not_held"
    assert not held(con)


def test_heartbeat_extends_a_held_lock(con):
    first = acquire_lock(con, NODE, "agent-a", "s1", "fix charge", ttl=60)
    res = renew_lock(con, NODE, "agent-a", "s1", ttl=600)
    assert res["renewed"] is True and res["expires_at"] > first["expires_at"]
    assert held(con)


def test_other_session_can_neither_renew_nor_release(con):
    first = acquire_lock(con, NODE, "agent-a", "s1", "fix charge", ttl=60)
    assert renew_lock(con, NODE, "agent-a", "s2", ttl=600)["renewed"] is False
    assert check_conflicts(con, [NODE])["locked"][NODE]["expires_at"] == first["expires_at"]
    rel = release_lock(con, NODE, "agent-a", "s2")
    assert rel["released"] is False and rel["reason"] == "not_owner"
    assert held(con)


def test_concurrent_heartbeats_and_release_leave_no_lock(tmp_path):
    db = tmp_path / "race.db"
    setup = get_connection(db)
    acquire_lock(setup, NODE, "agent-a", "s1", "fix charge")
    released = threading.Event()
    errors = []

    def heartbeat():
        c = get_connection(db)
        try:
            for _ in range(60):
                renew_lock(c, NODE, "agent-a", "s1")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    def release():
        c = get_connection(db)
        try:
            release_lock(c, NODE, "agent-a", "s1")
            released.set()
        finally:
            c.close()

    threads = [threading.Thread(target=heartbeat) for _ in range(3)] + [threading.Thread(target=release)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert released.is_set() and not errors
    assert not held(setup)
    setup.close()
