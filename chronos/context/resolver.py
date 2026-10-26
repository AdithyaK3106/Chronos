"""Deterministic Zero-Path Context Resolution Engine.

Extracts features from natural-language task prompts, performs lexical FTS5
matching over the repository AST knowledge graph, discovers co-located test
suites, and formats explainable provenance rationales for Turn 1 injection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from chronos.context.graph import GraphStore

# Common English stop words to filter from natural-language task prompts
STOP_WORDS: Set[str] = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but",
    "by", "can", "did", "do", "does", "doing", "don", "down", "during", "each", "few", "for",
    "from", "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "if", "in", "into", "is", "it", "its", "itself", "just",
    "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "of", "off", "on", "once",
    "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "s", "same", "she",
    "should", "so", "some", "such", "t", "than", "that", "the", "their", "theirs", "them",
    "themselves", "then", "there", "these", "they", "this", "those", "through", "to", "too",
    "under", "until", "up", "very", "was", "we", "were", "what", "when", "where", "which",
    "while", "who", "whom", "why", "will", "with", "would", "you", "your", "yours", "yourself",
    # Generic task directive verbs to deprioritize
    "please", "fix", "update", "modify", "implement", "add", "remove", "change", "refactor",
}


@dataclass
class HarvestedFeatures:
    raw_prompt: str
    explicit_paths: list[str] = field(default_factory=list)
    stack_trace_files: list[str] = field(default_factory=list)
    camel_case_symbols: list[str] = field(default_factory=list)
    snake_case_symbols: list[str] = field(default_factory=list)
    dotted_symbols: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    @property
    def all_search_terms(self) -> list[str]:
        terms = set()
        terms.update(self.explicit_paths)
        terms.update(self.stack_trace_files)
        # Add path stems
        for p in self.explicit_paths + self.stack_trace_files:
            terms.add(Path(p).stem)
        terms.update(self.camel_case_symbols)
        terms.update(self.snake_case_symbols)
        for d in self.dotted_symbols:
            terms.update(d.split("."))
        terms.update(self.exceptions)
        terms.update(self.keywords)
        return [t for t in terms if len(t) >= 3 and t.lower() not in STOP_WORDS]


@dataclass
class CandidateEvidence:
    node_id: str
    name: str
    path: str
    kind: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class ResolutionResult:
    target_files: list[str]
    seed_symbols: list[str]
    test_files: list[str]
    candidates: list[CandidateEvidence]
    provenance_summary: str


class DeterministicContextResolver:
    """Zero-path context resolver mapping natural language prompts to repository AST nodes."""

    def __init__(
        self,
        repo_root: str | Path,
        graph_store: GraphStore,
        slm_resolver: Optional[Any] = None,
        enable_slm: bool = False,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.graph_store = graph_store
        if slm_resolver is not None:
            self.slm_resolver = slm_resolver
        elif enable_slm:
            try:
                from chronos.context.slm_resolver import LocalSLMResolver
                self.slm_resolver = LocalSLMResolver(repo_root=self.repo_root)
            except Exception:
                self.slm_resolver = None
        else:
            self.slm_resolver = None

    def harvest_features(self, prompt: str) -> HarvestedFeatures:
        """Extract deterministic features using regex patterns."""
        explicit_paths = re.findall(r'(?:[\w\-./]+/)?[\w\-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|go|json|yaml|yml|sql|sh)', prompt)
        stack_traces = re.findall(r'File\s+"([^"]+)",\s+line\s+(\d+)', prompt)
        st_files = [m[0] for m in stack_traces]
        dotted_symbols = re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b', prompt)
        camel_cases = re.findall(r'\b[A-Z][a-zA-Z0-9]{2,}\b', prompt)
        snake_cases = re.findall(r'\b[a-z_][a-z0-9_]{2,}\b', prompt)
        exceptions = re.findall(r'\b[A-Z]\w*(?:Error|Exception|Fault|Panic)\b', prompt)

        # Tokenize prompt words and remove stop words
        words = re.findall(r'\b[a-zA-Z0-9_]{3,}\b', prompt.lower())
        keywords = [w for w in words if w not in STOP_WORDS]

        return HarvestedFeatures(
            raw_prompt=prompt,
            explicit_paths=explicit_paths,
            stack_trace_files=st_files,
            camel_case_symbols=camel_cases,
            snake_case_symbols=[s for s in snake_cases if s not in STOP_WORDS],
            dotted_symbols=dotted_symbols,
            exceptions=exceptions,
            keywords=keywords,
        )

    def resolve(
        self,
        prompt: str,
        explicit_target_files: Optional[list[str]] = None,
        max_seeds: int = 5,
    ) -> ResolutionResult:
        """Resolve relevant files and AST symbols from prompt."""
        features = self.harvest_features(prompt)
        candidates_map: Dict[str, CandidateEvidence] = {}

        # 1. If explicit paths are provided, prioritize them
        if explicit_target_files:
            for tf in explicit_target_files:
                norm_tf = Path(tf).as_posix()
                candidates_map[norm_tf] = CandidateEvidence(
                    node_id=norm_tf,
                    name=Path(tf).stem,
                    path=norm_tf,
                    kind="File",
                    score=100.0,
                    reasons=["Explicitly specified by task configuration"],
                )

        # 1.5. SLM Semantic Extraction (if enabled and no explicit paths are provided)
        if self.slm_resolver and not explicit_target_files and not features.explicit_paths:
            try:
                slm_res = self.slm_resolver.resolve_prompt(prompt)
                if slm_res and slm_res.suspected_files:
                    for sf in slm_res.suspected_files:
                        norm_sf = Path(sf).as_posix()
                        if norm_sf not in candidates_map:
                            candidates_map[norm_sf] = CandidateEvidence(
                                node_id=norm_sf,
                                name=Path(sf).stem,
                                path=norm_sf,
                                kind="File",
                                score=65.0,
                                reasons=[f"SLM Semantic Inference: {slm_res.rationale or 'Target file identified'}"],
                            )
                    for sym in slm_res.suspected_symbols:
                        if sym not in features.keywords:
                            features.keywords.append(sym)
                    for conc in slm_res.domain_concepts:
                        if conc not in features.keywords:
                            features.keywords.append(conc)
            except Exception:
                pass

        # 2. Score explicit paths extracted via regex
        for p in features.explicit_paths + features.stack_trace_files:
            norm_p = Path(p).as_posix()
            if norm_p not in candidates_map:
                candidates_map[norm_p] = CandidateEvidence(
                    node_id=norm_p,
                    name=Path(p).stem,
                    path=norm_p,
                    kind="File",
                    score=50.0,
                    reasons=[f"Explicit path or stack trace in prompt: '{p}'"],
                )

        # 3. Direct lookup for identified symbols / dotted parts
        direct_symbols = set(features.camel_case_symbols + features.snake_case_symbols)
        for d in features.dotted_symbols:
            direct_symbols.update(d.split("."))

        for sym in direct_symbols:
            if len(sym) < 3 or sym.lower() in STOP_WORDS:
                continue
            matched_nodes = self.graph_store.get_nodes_by_name(sym)
            for mn in matched_nodes:
                nid = mn["id"]
                score = 30.0
                reasons = [f"Exact symbol identifier match for '{sym}'"]
                if nid in candidates_map:
                    candidates_map[nid].score += score
                    candidates_map[nid].reasons.extend(reasons)
                else:
                    candidates_map[nid] = CandidateEvidence(
                        node_id=nid,
                        name=mn["name"],
                        path=mn["path"],
                        kind=mn.get("kind", "Symbol"),
                        score=score,
                        reasons=reasons,
                    )

        # 4. Query FTS5 lexical index for symbol candidates
        search_terms = features.all_search_terms
        fts_matches = self.graph_store.query_symbols_lexical(search_terms, limit=25)
        prompt_lower = prompt.lower()

        for m in fts_matches:
            node_id = m["id"]
            name = m["name"]
            path = m["path"]
            reasons = []
            score = 0.0

            # Exact symbol name in prompt
            if name.lower() in prompt_lower or any(name.lower() == t.lower() for t in search_terms):
                score += 25.0
                reasons.append(f"Exact symbol match for '{name}' in task prompt")

            # Path stem match
            stem = Path(path).stem.lower()
            if stem in prompt_lower or any(stem == t.lower() for t in search_terms):
                score += 15.0
                reasons.append(f"File stem '{stem}' matches prompt term")

            # BM25 rank contribution (lower bm25 score means higher relevance in SQLite)
            bm25_rank = m.get("bm25_rank", 0.0)
            score += max(0.0, 10.0 - abs(bm25_rank))
            reasons.append(f"Lexical match (BM25 rank: {bm25_rank:.2f})")

            # Docstring / signature matching bonus
            doc = (m.get("docstring") or "").lower()
            sig = (m.get("signature") or "").lower()
            if any(t.lower() in doc or t.lower() in sig for t in search_terms if len(t) >= 4):
                score += 5.0
                reasons.append("Signature/docstring matches task keywords")

            if node_id in candidates_map:
                candidates_map[node_id].score += score
                candidates_map[node_id].reasons.extend(reasons)
            else:
                candidates_map[node_id] = CandidateEvidence(
                    node_id=node_id,
                    name=name,
                    path=path,
                    kind=m.get("kind", "Symbol"),
                    score=score,
                    reasons=reasons,
                )

        # 5. Filter by minimum relevance threshold and sort
        valid_candidates = [c for c in candidates_map.values() if c.score >= 5.0]
        sorted_candidates = sorted(valid_candidates, key=lambda c: c.score, reverse=True)
        top_candidates = sorted_candidates[:max_seeds]

        target_files_set: Set[str] = set()
        seed_symbols_set: Set[str] = set()

        for c in top_candidates:
            if c.path:
                target_files_set.add(c.path)
            if c.kind != "File":
                seed_symbols_set.add(c.name)

        # 6. Discover co-located / matching unit test files
        test_files_set: Set[str] = set()
        test_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".go"}
        tests_dir = self.repo_root / "tests"

        for tf in target_files_set:
            tf_path = Path(tf)
            stem = tf_path.stem.split(".")[0]
            parent_stem = tf_path.parent.name.lower()

            # Co-located tests (standard in Go and TypeScript/React)
            parent_dir = (self.repo_root / tf_path.parent).resolve()
            if parent_dir.is_dir():
                for candidate_name in (
                    f"{stem}_test.go",
                    f"{stem}.test.ts",
                    f"{stem}.test.tsx",
                    f"{stem}.spec.ts",
                    f"{stem}.spec.tsx",
                    f"{stem}.test.js",
                    f"{stem}.spec.js",
                    f"test_{stem}.py",
                ):
                    co_test = parent_dir / candidate_name
                    if co_test.is_file():
                        try:
                            test_files_set.add(co_test.relative_to(self.repo_root).as_posix())
                        except ValueError:
                            pass

            # Centralized tests/ directory
            if tests_dir.is_dir():
                for test_file in tests_dir.rglob("*"):
                    if test_file.is_file() and test_file.suffix.lower() in test_exts:
                        t_stem = test_file.stem.lower()
                        if (
                            t_stem == f"test_{stem}"
                            or t_stem == f"{stem}_test"
                            or t_stem == f"{stem}.test"
                            or t_stem == f"{stem}.spec"
                            or t_stem.startswith(f"test_{stem}_")
                            or f"_{stem}" in t_stem
                            or f"{stem}_" in t_stem
                            or (parent_stem and parent_stem not in ("src", "lib", "chronos") and (
                                t_stem == f"test_{parent_stem}"
                                or t_stem == f"{parent_stem}_test"
                                or f"_{parent_stem}" in t_stem
                                or f"{parent_stem}_" in t_stem
                            ))
                        ):
                            try:
                                rel_p = test_file.relative_to(self.repo_root).as_posix()
                                test_files_set.add(rel_p)
                            except ValueError:
                                pass

        # 7. Build provenance rationale summary
        provenance_lines = ["#### 🔍 Retrieval Provenance & Relevance Rationale:"]
        if top_candidates:
            for c in top_candidates[:3]:
                reasons_str = "; ".join(c.reasons[:2])
                provenance_lines.append(f"- **`{c.name}`** (`{c.path}`): Score {c.score:.1f} — {reasons_str}")
        else:
            provenance_lines.append("- No high-confidence symbols matched; relying on general repository governance.")

        if test_files_set:
            provenance_lines.append(f"- **Associated Tests Found:** {', '.join([f'`{t}`' for t in sorted(test_files_set)[:3]])}")

        return ResolutionResult(
            target_files=sorted(target_files_set),
            seed_symbols=sorted(seed_symbols_set),
            test_files=sorted(test_files_set),
            candidates=top_candidates,
            provenance_summary="\n".join(provenance_lines),
        )
