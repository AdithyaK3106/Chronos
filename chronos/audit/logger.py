"""Tamper-evident append-only SHA-256 hash-chained audit logging (F4)."""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chronos.security.sanitizer import redact_secrets

GENESIS_HASH = "0" * 64
_append_lock = threading.Lock()

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _canonical_json(data: dict[str, Any]) -> str:
    """Deterministic JSON serialization for hashing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))

def _compute_hash(prev_hash: str, payload_json: str) -> str:
    hasher = hashlib.sha256()
    hasher.update(prev_hash.encode("utf-8"))
    hasher.update(payload_json.encode("utf-8"))
    return hasher.hexdigest()

def _read_last_hash(log_file: Path) -> str:
    """Read the hash of the last entry in the log file, or GENESIS_HASH if empty/missing."""
    if not log_file.is_file() or log_file.stat().st_size == 0:
        return GENESIS_HASH

    with open(log_file, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(4096, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            if buf.count(b"\n") >= 2 or pos == 0:
                break
        lines = [line.strip() for line in buf.splitlines() if line.strip()]
        if not lines:
            return GENESIS_HASH
        try:
            last_entry = json.loads(lines[-1].decode("utf-8"))
            return last_entry.get("hash", GENESIS_HASH)
        except Exception:
            return GENESIS_HASH

def append_audit_event(
    log_path: str | Path,
    event_type: str,
    agent_id: str,
    action: str,
    target: str,
    status: str,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a tamper-evident audit record to the log. Redacts all secrets before writing."""
    p = Path(log_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)

    cleaned_metadata = redact_secrets(metadata or {})
    cleaned_target = redact_secrets(target)
    cleaned_reason = redact_secrets(reason)

    with _append_lock:
        prev_hash = _read_last_hash(p)
        payload = {
            "timestamp": _utc_now_iso(),
            "event_type": event_type,
            "agent_id": agent_id,
            "action": action,
            "target": cleaned_target,
            "status": status,
            "reason": cleaned_reason,
            "metadata": cleaned_metadata,
            "prev_hash": prev_hash,
        }
        payload_json = _canonical_json(payload)
        entry_hash = _compute_hash(prev_hash, payload_json)
        payload["hash"] = entry_hash

        line = json.dumps(payload) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

        return payload

def verify_audit_chain(log_path: str | Path) -> tuple[bool, str, int]:
    """Verify that an audit log has not been tampered with.
    
    Returns:
        (is_valid, report_or_error, total_events)
    """
    p = Path(log_path).resolve()
    if not p.is_file():
        return True, "Log file does not exist (empty valid log)", 0

    expected_prev = GENESIS_HASH
    count = 0

    with open(p, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, start=1):
            line_str = line.strip()
            if not line_str:
                continue

            try:
                entry = json.loads(line_str)
            except Exception as e:
                return False, f"Line {idx}: JSON parse error: {e}", count

            recorded_prev = entry.get("prev_hash")
            recorded_hash = entry.get("hash")

            if recorded_prev != expected_prev:
                return False, f"Line {idx}: Broken hash chain! Expected prev_hash {expected_prev}, got {recorded_prev}", count

            # Recompute hash
            payload_to_hash = dict(entry)
            payload_to_hash.pop("hash", None)
            payload_json = _canonical_json(payload_to_hash)
            computed = _compute_hash(expected_prev, payload_json)

            if computed != recorded_hash:
                return False, f"Line {idx}: Hash mismatch! Computed {computed}, stored {recorded_hash}", count

            expected_prev = recorded_hash
            count += 1

    return True, f"Verified {count} events with intact cryptographic chain", count
