"""Chronos Epistemic Living Memory Store.

SQLite-backed persistent memory engine with FTS5 lexical ranking,
recency decay, epistemic verification weighting, and repository namespace isolation.
Adapted from oxgeneral/agentmem (MIT License) with Chronos governance integration.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from chronos.core.db import get_connection
from chronos.memory.embeddings import EmbeddingBackend
from chronos.memory.models import (
    MemoryRecord,
    MemoryStats,
    MemoryTier,
    OutcomeType,
    RecallResult,
    VerificationStatus,
)
from chronos.security.sanitizer import redact_secrets


_EN_STOP = frozenset({
    "the", "is", "a", "an", "to", "of", "in", "for", "on", "at",
    "by", "it", "or", "and", "be", "do", "if", "so", "as", "no",
    "up", "my", "we", "he", "she", "me", "us", "its", "was", "are",
    "has", "had", "not", "but", "you", "i", "am", "did", "got",
    "all", "any", "can", "may", "will", "with", "this", "that",
    "have", "from", "they", "them", "then", "than", "been", "were",
    "how", "what", "where", "when", "which", "who", "why",
})


def _escape_like(s: str) -> str:
    """Escape LIKE wildcard characters (% and _) so they match literally."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _iso_to_epoch(iso_str: str) -> float:
    """Parse ISO timestamp to unix epoch, defaulting to now on failure."""
    if not iso_str:
        return time.time()
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.timestamp()
    except Exception:
        return time.time()


def _extract_entities_from_text(text: str) -> List[Tuple[str, str]]:
    """Heuristic extractor for file paths, code symbols, and env vars."""
    entities: List[Tuple[str, str]] = []
    seen = set()

    # File paths (e.g. chronos/core/db.py, tests/test_memory.py)
    path_matches = re.findall(r"\b([a-zA-Z0-9_\-\./]+\.[a-zA-Z0-9_\-]+)\b", text)
    for p in path_matches:
        if "/" in p and p not in seen and len(p) > 3:
            seen.add(p)
            entities.append((p, "file"))

    # Environment variables (e.g. OPENHANDS_SUPPRESS_BANNER, CHRONOS_MODEL)
    env_matches = re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text)
    for env in env_matches:
        if env not in seen and not env.startswith("TEST"):
            seen.add(env)
            entities.append((env, "env_var"))

    # Code symbols: CamelCase or snake_case with parentheses (e.g. SQLiteGraphStore, as_of_callers)
    sym_matches = re.findall(r"\b([A-Z][a-zA-Z0-9]{3,}|[a-z]{2,}_[a-z0-9_]{2,})\b", text)
    for sym in sym_matches:
        if sym not in seen and len(sym) >= 4:
            seen.add(sym)
            entities.append((sym, "symbol"))

    return entities


class ChronosMemoryStore:
    """Persistent Epistemic Living Memory Store with FTS5 lexical retrieval."""

    def __init__(
        self,
        con: Optional[sqlite3.Connection] = None,
        db_path: Union[str, Path] = ":memory:",
        embedding_backend: Optional[EmbeddingBackend] = None,
    ):
        self.con = con or get_connection(db_path)
        self.embedding_backend = embedding_backend

    def remember(
        self,
        content: str,
        repo_namespace: str = "",
        tier: Union[MemoryTier, str] = MemoryTier.LEARNED,
        summary: str = "",
        source: str = "manual",
        task_id: str = "",
        agent_id: str = "",
        outcome: Union[OutcomeType, str] = OutcomeType.SUCCESS,
        verification_status: Union[VerificationStatus, str] = VerificationStatus.UNVERIFIED,
        confidence: float = 0.8,
        importance: float = 0.5,
        evidence: str = "",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        supersedes: Optional[int] = None,
    ) -> int:
        """Store a single memory record with automatic secret scrubbing and entity extraction."""
        rec = MemoryRecord(
            repo_namespace=repo_namespace,
            tier=MemoryTier(tier),
            content=str(content).strip(),
            summary=str(summary).strip(),
            source=source,
            task_id=task_id,
            agent_id=agent_id,
            outcome=OutcomeType(outcome),
            verification_status=VerificationStatus(verification_status),
            confidence=max(0.0, min(1.0, float(confidence))),
            importance=max(0.0, min(1.0, float(importance))),
            evidence=str(evidence).strip(),
            tags=tags or [],
            metadata=metadata or {},
            supersedes=supersedes,
        )
        return self._insert_record(rec)

    def remember_batch(
        self,
        records: List[Union[MemoryRecord, Dict[str, Any]]],
        repo_namespace: Optional[str] = None,
    ) -> List[int]:
        """Insert multiple memory records atomically within a transaction."""
        inserted_ids: List[int] = []
        if not records:
            return inserted_ids

        with self.con:
            for item in records:
                if isinstance(item, dict):
                    data = dict(item)
                    if repo_namespace and not data.get("repo_namespace"):
                        data["repo_namespace"] = repo_namespace
                    rec = MemoryRecord(**data)
                else:
                    rec = item
                    if repo_namespace and not rec.repo_namespace:
                        rec.repo_namespace = repo_namespace

                mid = self._insert_record(rec, in_transaction=True)
                inserted_ids.append(mid)
        return inserted_ids

    def _insert_record(self, record: MemoryRecord, in_transaction: bool = False) -> int:
        """Internal worker to scrub secrets, insert into memories, and index entities."""
        # Clean and scrub secrets from untrusted inputs
        clean_content = redact_secrets(record.content)
        clean_summary = redact_secrets(record.summary or clean_content.split("\n")[0][:120])
        clean_evidence = redact_secrets(record.evidence)
        tags_json = json.dumps(record.tags)
        meta_json = json.dumps(redact_secrets(record.metadata))

        cur = self.con.execute(
            """
            INSERT INTO memories (
                repo_namespace, tier, content, summary, source, task_id, agent_id,
                outcome, verification_status, confidence, importance, evidence,
                tags, metadata, supersedes, archived, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.repo_namespace,
                record.tier.value if hasattr(record.tier, "value") else str(record.tier),
                clean_content,
                clean_summary,
                record.source,
                record.task_id,
                record.agent_id,
                record.outcome.value if hasattr(record.outcome, "value") else str(record.outcome),
                record.verification_status.value
                if hasattr(record.verification_status, "value")
                else str(record.verification_status),
                record.confidence,
                record.importance,
                clean_evidence,
                tags_json,
                meta_json,
                record.supersedes,
                1 if record.archived else 0,
                record.created_at,
                record.updated_at,
            ),
        )
        memory_id = cur.lastrowid
        assert memory_id is not None

        # Extract and index referenced entities
        entities = _extract_entities_from_text(f"{clean_content} {clean_evidence} {' '.join(record.tags)}")
        for ent_name, ent_type in entities:
            self.con.execute(
                "INSERT INTO entities (name, type, memory_id) VALUES (?, ?, ?)",
                (ent_name, ent_type, memory_id),
            )

        if not in_transaction:
            self.con.commit()
        return memory_id

    def update_memory(
        self,
        old_id: int,
        new_content: str,
        summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        importance: Optional[float] = None,
    ) -> int:
        """Update an existing memory with supersedes lineage tracking and archive the old version."""
        row = self.con.execute("SELECT * FROM memories WHERE id = ?", (old_id,)).fetchone()
        if not row:
            raise KeyError(f"Memory with ID {old_id} does not exist")

        with self.con:
            # 1. Archive previous version
            self.con.execute(
                "UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), old_id),
            )

            # 2. Inherit properties from old record unless overridden
            tags = json.loads(row["tags"]) if row["tags"] else []
            old_meta = json.loads(row["metadata"]) if row["metadata"] else {}
            if metadata:
                old_meta.update(metadata)

            new_rec = MemoryRecord(
                repo_namespace=row["repo_namespace"],
                tier=MemoryTier(row["tier"]),
                content=new_content,
                summary=summary or (new_content.split("\n")[0][:120]),
                source=row["source"],
                task_id=row["task_id"],
                agent_id=row["agent_id"],
                outcome=OutcomeType(row["outcome"]),
                verification_status=VerificationStatus(row["verification_status"]),
                confidence=row["confidence"],
                importance=importance if importance is not None else row["importance"],
                evidence=row["evidence"],
                tags=tags,
                metadata=old_meta,
                supersedes=old_id,
            )
            new_id = self._insert_record(new_rec, in_transaction=True)
        return new_id

    def archive_memory(self, memory_id: int) -> bool:
        """Mark a memory as archived so it is excluded from search by default."""
        cur = self.con.execute(
            "UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), memory_id),
        )
        self.con.commit()
        return cur.rowcount > 0

    def delete_memory(self, memory_id: int) -> bool:
        """Hard delete a memory record and associated entities and FTS entries."""
        with self.con:
            self.con.execute("DELETE FROM entities WHERE memory_id = ?", (memory_id,))
            cur = self.con.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def recall(
        self,
        query: str,
        repo_namespace: str = "",
        tier: Optional[Union[MemoryTier, str]] = None,
        limit: int = 5,
        min_score: float = 0.0,
        recency_weight: float = 0.15,
        decay_rate: float = 0.01,
        current_only: bool = True,
    ) -> List[RecallResult]:
        """Hybrid/lexical search returning ranked memories matching query within namespace."""
        clean_query = query.strip()
        if not clean_query:
            return []

        fts_pattern = self._build_fts_query(clean_query)
        candidates: List[Dict[str, Any]] = []

        if fts_pattern:
            candidates = self._fts_search(
                fts_query=fts_pattern,
                repo_namespace=repo_namespace,
                tier=tier,
                limit=limit * 3,
                current_only=current_only,
            )

        # Fallback to LIKE keyword search if FTS5 yielded zero candidates or query was tricky
        if not candidates:
            candidates = self._like_search(
                raw_query=clean_query,
                repo_namespace=repo_namespace,
                tier=tier,
                limit=limit * 3,
                current_only=current_only,
            )

        # Rank and score candidates
        now_epoch = time.time()
        scored_results: List[RecallResult] = []

        # Min-max normalization for FTS5 BM25 ranks
        has_ranks = [c["rank"] for c in candidates if c.get("rank") is not None]
        min_rank = min(has_ranks) if has_ranks else 0.0
        max_rank = max(has_ranks) if has_ranks else 0.0
        rank_range = max_rank - min_rank if max_rank != min_rank else 1.0

        for cand in candidates:
            raw_rank = cand.get("rank")
            if raw_rank is not None and rank_range > 0:
                # BM25 is negative: most negative is strongest match -> maps to [0.2 .. 1.0]
                norm_score = (max_rank - raw_rank) / rank_range
                lexical_score = 0.2 + 0.8 * norm_score
            elif raw_rank is not None:
                lexical_score = 1.0
            else:
                lexical_score = 0.5  # standard baseline for LIKE fallback

            # 2. Recency score
            created_epoch = _iso_to_epoch(cand["created_at"])
            age_hours = max(0.0, (now_epoch - created_epoch) / 3600.0)
            recency_score = math.exp(-age_hours * decay_rate)

            # 3. Base hybrid score
            base_score = lexical_score * (1.0 - recency_weight) + recency_score * recency_weight

            # 4. Importance multiplier [0.8 .. 1.2]
            importance = float(cand.get("importance", 0.5))
            importance_mult = 0.8 + 0.4 * importance

            # 5. Verification status multiplier
            verif_status = cand.get("verification_status", "unverified")
            outcome = cand.get("outcome", "success")
            if verif_status == "verified":
                verif_mult = 1.3
            elif outcome == "failed_hypothesis":
                verif_mult = 0.85
            else:
                verif_mult = 1.0

            final_score = base_score * importance_mult * verif_mult
            if final_score < min_score:
                continue

            scored_results.append(
                RecallResult(
                    id=cand["id"],
                    content=cand["content"],
                    summary=cand.get("summary", ""),
                    tier=MemoryTier(cand["tier"]),
                    source=cand.get("source", ""),
                    repo_namespace=cand.get("repo_namespace", ""),
                    score=round(final_score, 4),
                    method="fts" if raw_rank is not None else "like",
                    importance=importance,
                    verification_status=VerificationStatus(verif_status),
                    outcome=OutcomeType(outcome),
                    evidence=cand.get("evidence", ""),
                    tags=json.loads(cand.get("tags") or "[]"),
                    metadata=json.loads(cand.get("metadata") or "{}"),
                    created_at=cand.get("created_at", ""),
                )
            )

        scored_results.sort(key=lambda r: r.score, reverse=True)
        return scored_results[:limit]

    def _build_fts_query(self, query: str) -> str:
        """Sanitize raw query into safe, high-precision FTS5 boolean terms."""
        cleaned = re.sub(r"[^\w\s]", " ", query, flags=re.UNICODE)
        raw_words = cleaned.split()
        if not raw_words:
            return ""

        # Filter stop words
        words = [w for w in raw_words if w.lower() not in _EN_STOP and len(w) > 1]
        if not words:
            words = [w for w in raw_words if len(w) > 1]
        if not words:
            return ""

        def token(w: str) -> str:
            return f"{w}*" if len(w) > 3 else w

        term_tokens = [token(w) for w in words]

        phrases = []
        if len(words) >= 2:
            for i in range(len(words) - 1):
                phrases.append(f'"{words[i]} {words[i+1]}"')

        parts = []
        if len(words) >= 2:
            parts.append('"' + " ".join(words[:6]) + '"')
        if len(words) >= 3:
            parts.extend(phrases[:4])
        if len(term_tokens) >= 2:
            parts.append(" AND ".join(term_tokens[:5]))
        parts.extend(term_tokens[:8])

        seen = set()
        unique_parts = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                unique_parts.append(p)
        return " OR ".join(unique_parts)

    def _fts_search(
        self,
        fts_query: str,
        repo_namespace: str,
        tier: Optional[Union[MemoryTier, str]],
        limit: int,
        current_only: bool,
    ) -> List[Dict[str, Any]]:
        """Run SQLite FTS5 query joined on memories table."""
        where_clauses = ["m.archived = 0"]
        params: List[Any] = [fts_query]

        if repo_namespace:
            where_clauses.append("(m.repo_namespace = ? OR m.repo_namespace = '__global__' OR m.repo_namespace = '')")
            params.append(repo_namespace)

        if tier:
            t_val = tier.value if hasattr(tier, "value") else str(tier)
            where_clauses.append("m.tier = ?")
            params.append(t_val)

        if current_only:
            where_clauses.append(
                "m.id NOT IN (SELECT supersedes FROM memories WHERE supersedes IS NOT NULL AND archived = 0)"
            )

        sql = f"""
            SELECT m.*, f.rank
            FROM memories_fts f
            JOIN memories m ON m.id = f.rowid
            WHERE memories_fts MATCH ? AND {" AND ".join(where_clauses)}
            ORDER BY f.rank LIMIT ?
        """
        params.append(limit)

        try:
            rows = self.con.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            # If FTS expression failed, return empty to trigger fallback
            return []

    def _like_search(
        self,
        raw_query: str,
        repo_namespace: str,
        tier: Optional[Union[MemoryTier, str]],
        limit: int,
        current_only: bool,
    ) -> List[Dict[str, Any]]:
        """Fallback LIKE matching when FTS5 produces zero candidates."""
        tokens = [t for t in re.split(r"[\s\W]+", raw_query) if len(t) >= 3]
        if not tokens:
            return []

        where_clauses = ["m.archived = 0"]
        params: List[Any] = []

        if repo_namespace:
            where_clauses.append("(m.repo_namespace = ? OR m.repo_namespace = '__global__' OR m.repo_namespace = '')")
            params.append(repo_namespace)

        if tier:
            t_val = tier.value if hasattr(tier, "value") else str(tier)
            where_clauses.append("m.tier = ?")
            params.append(t_val)

        if current_only:
            where_clauses.append(
                "m.id NOT IN (SELECT supersedes FROM memories WHERE supersedes IS NOT NULL AND archived = 0)"
            )

        like_clauses = []
        for t in tokens[:3]:
            like_clauses.append("(m.content LIKE ? OR m.summary LIKE ? OR m.tags LIKE ?)")
            pat = f"%{_escape_like(t)}%"
            params.extend([pat, pat, pat])

        where_clauses.append(f"({' OR '.join(like_clauses)})")
        sql = f"SELECT m.*, NULL as rank FROM memories m WHERE {' AND '.join(where_clauses)} ORDER BY m.id DESC LIMIT ?"
        params.append(limit)

        try:
            rows = self.con.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_procedures(self, repo_namespace: str = "") -> List[str]:
        """Retrieve active procedural memories formatted as rules for system prompt injection."""
        where_clauses = ["tier = 'procedural'", "archived = 0"]
        params: List[Any] = []

        if repo_namespace:
            where_clauses.append("(repo_namespace = ? OR repo_namespace = '__global__' OR repo_namespace = '')")
            params.append(repo_namespace)

        rows = self.con.execute(
            f"SELECT content FROM memories WHERE {' AND '.join(where_clauses)} ORDER BY id ASC",
            params,
        ).fetchall()
        return [r["content"] for r in rows]

    def add_procedure(self, rule: str, repo_namespace: str = "") -> int:
        """Add a procedural rule or behavioral invariant."""
        return self.remember(
            content=rule,
            repo_namespace=repo_namespace,
            tier=MemoryTier.PROCEDURAL,
            source="manual",
            importance=0.9,
            verification_status=VerificationStatus.VERIFIED,
        )

    def save_state(self, state: str, repo_namespace: str = "") -> int:
        """Save ephemeral working state for context preservation."""
        return self.remember(
            content=state,
            repo_namespace=repo_namespace,
            tier=MemoryTier.WORKING,
            source="working_state",
            importance=0.7,
        )

    def get_state(self, repo_namespace: str = "") -> List[MemoryRecord]:
        """Retrieve recent non-archived working state items."""
        return self.list_memories(
            repo_namespace=repo_namespace,
            tier=MemoryTier.WORKING,
            limit=5,
            include_archived=False,
        )

    def list_memories(
        self,
        repo_namespace: str = "",
        tier: Optional[Union[MemoryTier, str]] = None,
        outcome: Optional[Union[OutcomeType, str]] = None,
        limit: int = 50,
        include_archived: bool = False,
    ) -> List[MemoryRecord]:
        """List stored memories with filtering options."""
        where_clauses = []
        params: List[Any] = []

        if not include_archived:
            where_clauses.append("archived = 0")

        if repo_namespace:
            where_clauses.append("(repo_namespace = ? OR repo_namespace = '__global__' OR repo_namespace = '')")
            params.append(repo_namespace)

        if tier:
            t_val = tier.value if hasattr(tier, "value") else str(tier)
            where_clauses.append("tier = ?")
            params.append(t_val)

        if outcome:
            o_val = outcome.value if hasattr(outcome, "value") else str(outcome)
            where_clauses.append("outcome = ?")
            params.append(o_val)

        where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = f"SELECT * FROM memories {where_str} ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = self.con.execute(sql, params).fetchall()
        records: List[MemoryRecord] = []
        for r in rows:
            records.append(
                MemoryRecord(
                    id=r["id"],
                    repo_namespace=r["repo_namespace"],
                    tier=MemoryTier(r["tier"]),
                    content=r["content"],
                    summary=r["summary"],
                    source=r["source"],
                    task_id=r["task_id"],
                    agent_id=r["agent_id"],
                    outcome=OutcomeType(r["outcome"]),
                    verification_status=VerificationStatus(r["verification_status"]),
                    confidence=r["confidence"],
                    importance=r["importance"],
                    evidence=r["evidence"],
                    tags=json.loads(r["tags"] or "[]"),
                    metadata=json.loads(r["metadata"] or "{}"),
                    supersedes=r["supersedes"],
                    archived=bool(r["archived"]),
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
            )
        return records

    def stats(self, repo_namespace: str = "") -> MemoryStats:
        """Compute aggregated memory statistics."""
        where = "WHERE (repo_namespace = ? OR repo_namespace = '__global__' OR repo_namespace = '')" if repo_namespace else ""
        params = [repo_namespace] if repo_namespace else []

        total_row = self.con.execute(f"SELECT count(*) as total FROM memories {where}", params).fetchone()
        total_count = total_row["total"] if total_row else 0

        arch_row = self.con.execute(
            f"SELECT count(*) as arch FROM memories {'WHERE archived = 1 AND ' + where[6:] if where else 'WHERE archived = 1'}",
            params,
        ).fetchone()
        archived_count = arch_row["arch"] if arch_row else 0

        by_tier_rows = self.con.execute(
            f"SELECT tier, count(*) as cnt FROM memories {where} GROUP BY tier",
            params,
        ).fetchall()
        by_tier = {r["tier"]: r["cnt"] for r in by_tier_rows}

        by_outcome_rows = self.con.execute(
            f"SELECT outcome, count(*) as cnt FROM memories {where} GROUP BY outcome",
            params,
        ).fetchall()
        by_outcome = {r["outcome"]: r["cnt"] for r in by_outcome_rows}

        ns_rows = self.con.execute("SELECT DISTINCT repo_namespace FROM memories WHERE repo_namespace != ''").fetchall()
        namespaces = [r[0] for r in ns_rows]

        return MemoryStats(
            total_memories=total_count,
            by_tier=by_tier,
            by_outcome=by_outcome,
            archived_count=archived_count,
            namespaces=namespaces,
        )
