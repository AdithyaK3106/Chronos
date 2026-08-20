"""Tamper-evident exportable audit log (F4).

Every ledger write also lands here as one JSON line in chronos-audit.log,
hash-chained so a single edited byte anywhere in the file is detectable.
Fire-and-forget: append() must never raise into the caller, and the file is
opened/closed per write so it survives a crash mid-write and can be copied
at any time without corruption.
"""

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import db

GENESIS = "0" * 64

# Guards the read-last-line -> compute-hash -> write sequence in append().
# Chronos's async tool calls can interleave within one process (that's the
# whole point of _track() serializing behind one server), so two concurrent
# writes without this lock can both read the same "last line" and produce
# two entries claiming the same prev_hash -- a broken chain, not a tamper.
_append_lock = threading.Lock()


def log_path() -> Path:
    return db.db_path().parent / "chronos-audit.log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _last_line(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path, "rb") as f:
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
        lines = buf.splitlines()
        return lines[-1].decode("utf-8") if lines else None


def _hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def append(event: dict, path: Path | None = None) -> None:
    """Append one hash-chained entry. Never raises."""
    try:
        with _append_lock:
            p = path or log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            last = _last_line(p)
            if last is None:
                seq, prev_hash = 1, GENESIS
            else:
                prev = json.loads(last)
                seq, prev_hash = prev["seq"] + 1, _hash_line(last)

            entry = dict(event)
            entry["seq"] = seq
            entry["prev_hash"] = prev_hash
            entry.setdefault("timestamp", _now())
            entry["entry_hash"] = ""
            entry["entry_hash"] = _hash_line(json.dumps(entry, sort_keys=True))

            line = json.dumps(entry, sort_keys=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
    except Exception as e:  # noqa: BLE001 -- audit failure must never break a tool call
        print(f"chronos audit: append failed: {type(e).__name__}: {e}", file=sys.stderr)


def _iter_segments(since_dir: Path) -> list[Path]:
    """Rotated segments (oldest first) followed by the live log."""
    rotated = sorted(since_dir.glob("chronos-audit-*.log"))
    live = since_dir / "chronos-audit.log"
    return rotated + ([live] if live.exists() else [])


def verify(since=None, until=None, path: Path | None = None) -> dict:
    """Recompute the hash chain across the live log and any rotated segments.

    Returns {"valid": bool, "checked": N, "broken_at": line_no_or_None, ...}.
    A rotation boundary is checked too: the new segment's first entry must
    chain from the previous segment's last line.
    """
    base = (path or log_path()).parent
    segments = _iter_segments(base)
    prev_hash = GENESIS
    checked = 0
    for seg in segments:
        with open(seg, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    return {"valid": False, "broken_at": (seg.name, lineno),
                             "reason": "invalid_json", "checked": checked}
                claimed_hash = entry.get("entry_hash", "")
                recomputed = dict(entry)
                recomputed["entry_hash"] = ""
                expected_hash = _hash_line(json.dumps(recomputed, sort_keys=True))
                if claimed_hash != expected_hash:
                    return {"valid": False, "broken_at": (seg.name, lineno),
                             "reason": "entry_hash_mismatch",
                             "expected": expected_hash, "actual": claimed_hash,
                             "checked": checked}
                if entry.get("prev_hash") != prev_hash:
                    return {"valid": False, "broken_at": (seg.name, lineno),
                             "reason": "chain_broken",
                             "expected": prev_hash, "actual": entry.get("prev_hash"),
                             "checked": checked}
                prev_hash = _hash_line(raw)
                checked += 1
    return {"valid": True, "broken_at": None, "checked": checked}


def _read_all(base: Path, since=None, until=None) -> list[dict]:
    out = []
    for seg in _iter_segments(base):
        with open(seg, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                entry = json.loads(raw)
                ts = entry.get("timestamp", "")
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue
                out.append(entry)
    return out


def export(fmt: str = "json", since=None, until=None, path: Path | None = None) -> str:
    base = (path or log_path()).parent
    entries = _read_all(base, since, until)
    if fmt == "json":
        return json.dumps(entries, indent=2)
    if fmt == "cef":
        lines = []
        for e in entries:
            cat = e.get("action") or e.get("event_type") or ""
            lines.append(
                f"CEF:0|Chronos|Chronos|1.0|{cat}|{cat}|3|"
                f"src={e.get('agent_id', '')} cat={cat} "
                f"fname={e.get('node_id', '')} rt={e.get('timestamp', '')} "
                f"externalId={e.get('seq', '')}"
            )
        return "\n".join(lines)
    raise ValueError(f"unknown format: {fmt}")


def stats(path: Path | None = None) -> dict:
    p = path or log_path()
    base = p.parent
    entries = _read_all(base)
    size = sum(seg.stat().st_size for seg in _iter_segments(base))
    return {
        "total_entries": len(entries),
        "date_range": [entries[0]["timestamp"], entries[-1]["timestamp"]] if entries else [None, None],
        "file_size_bytes": size,
        "last_entry_at": entries[-1]["timestamp"] if entries else None,
    }


def maybe_rotate(max_mb: float | None = None, path: Path | None = None) -> str | None:
    """Rotate the live log if it exceeds CHRONOS_AUDIT_MAX_MB. Returns the
    rotated filename, or None if no rotation happened."""
    p = path or log_path()
    if not p.exists():
        return None
    limit = max_mb if max_mb is not None else float(os.environ.get("CHRONOS_AUDIT_MAX_MB", "100"))
    if p.stat().st_size < limit * 1_048_576:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    dest = p.parent / f"chronos-audit-{ts}.log"
    p.rename(dest)
    return dest.name
