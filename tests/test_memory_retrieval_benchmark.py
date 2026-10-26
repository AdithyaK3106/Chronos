"""Fixed retrieval benchmark for Chronos Epistemic Living Memory.

Evaluates precision@1, precision@3, precision@5, and Mean Reciprocal Rank (MRR)
across a suite of realistic code queries with ground-truth targets and distractors.
"""

import time
import pytest
from pathlib import Path

from chronos.core.db import get_connection
from chronos.memory.models import MemoryTier, VerificationStatus
from chronos.memory.store import ChronosMemoryStore

BENCHMARK_GROUND_TRUTH = [
    {
        "query": "How to prevent infinite cycle loop in recursive CTE graph traversal?",
        "target_content": "Cycle prevention in SQLiteGraphStore requires tracking visited path strings inside recursive CTE queries.",
        "tags": ["graph", "cte", "cycle_detection"],
        "tier": MemoryTier.LEARNED,
    },
    {
        "query": "Resolving relative imports with multiple dots in Python packages",
        "target_content": "Python relative import resolution requires calculating dot levels from source directory to project root.",
        "tags": ["python", "imports", "linker"],
        "tier": MemoryTier.LEARNED,
    },
    {
        "query": "Handling nil interface comparisons in Go struct pointers",
        "target_content": "In Go, a nil concrete pointer assigned to an interface causes interface != nil checks to evaluate true.",
        "tags": ["go", "interfaces", "nil_pointer"],
        "tier": MemoryTier.LEARNED,
    },
    {
        "query": "Configuring SQLite WAL mode for high concurrency write throughput",
        "target_content": "SQLite WAL mode with PRAGMA synchronous = NORMAL and busy_timeout = 30000 maximizes concurrent read/write throughput.",
        "tags": ["sqlite", "wal", "concurrency"],
        "tier": MemoryTier.CORE,
    },
    {
        "query": "Path alias resolution for tsconfig compilerOptions in monorepos",
        "target_content": "TypeScript path aliases in tsconfig compilerOptions paths must match base directory prefixes and probe index files.",
        "tags": ["typescript", "tsconfig", "monorepo"],
        "tier": MemoryTier.LEARNED,
    },
    {
        "query": "Scrubbing bearer authorization tokens and API credentials",
        "target_content": "Credential scrubbing regex pattern for bearer tokens must redact auth headers before log persistence.",
        "tags": ["security", "sanitizer", "secrets"],
        "tier": MemoryTier.PROCEDURAL,
    },
    {
        "query": "Handling CancellationToken in OpenHands tool execution loop",
        "target_content": "OpenHands CancellationToken must be polled before long-running file operations to respect user stop signals.",
        "tags": ["openhands", "cancellation", "runtime"],
        "tier": MemoryTier.LEARNED,
    },
    {
        "query": "Sub-file AST write lease renewal and TTL expiration",
        "target_content": "Chronos sub-file AST write leases automatically expire after 300s TTL unless explicitly renewed by holder agent.",
        "tags": ["coordination", "locks", "leases"],
        "tier": MemoryTier.CORE,
    },
]

BENCHMARK_DISTRACTORS = [
    "CSS flexbox centering using justify-content center and align-items center.",
    "Tailwind CSS v4 arbitrary value syntax using square brackets.",
    "Kubernetes ingress controller configuration for TLS termination.",
    "Configuring Docker multi-stage builds with alpine linux base images.",
    "PostgreSQL advisory locks compared to row-level select for update.",
    "Redis cluster hash slots distribution across master nodes.",
    "React server components data serialization across client boundaries.",
    "Next.js app router streaming SSR using suspense boundaries.",
    "GraphQL schema stitching and federation across microservices.",
    "Prometheus metric histogram bucket configuration for p99 latency.",
    "Kafka consumer group rebalancing protocol and cooperative sticky assignor.",
    "Elasticsearch inverted index segment merging and refresh intervals.",
    "Terraform state file remote backend locking on S3 with DynamoDB.",
    "WebAssembly memory allocation and shared buffer access from JavaScript.",
    "Configuring Nginx reverse proxy buffer sizes for large HTTP headers.",
    "OpenTelemetry trace context propagation over HTTP W3C headers.",
    "gRPC connection pooling and keepalive ping intervals on HTTP2 transport.",
    "Linux systemd service unit restarting on failure with restart-sec.",
    "Git interactive rebase resolving merge conflicts with rerere enabled.",
    "Babel AST transformation plugin visitor pattern for JSX elements.",
    "Webpack code splitting using dynamic import statements and chunk names.",
    "Vite hot module replacement websocket protocol and module invalidation.",
    "Electron IPC communication between main process and renderer preload.",
    "Chromium CDP page screenshot viewport clipping coordinates.",
    "SQLite JSON1 json_extract function extracting nested array elements.",
    "Python asyncio event loop running synchronous blocking calls via to_thread.",
    "Go channel buffering semantics when sending to closed channels.",
]


@pytest.fixture
def populated_bench_store(tmp_path: Path):
    db_file = tmp_path / "bench.db"
    con = get_connection(db_file)
    store = ChronosMemoryStore(con=con)

    # 1. Populate ground truth targets
    for item in BENCHMARK_GROUND_TRUTH:
        mid = store.remember(
            content=item["target_content"],
            repo_namespace="bench_repo",
            tier=item["tier"],
            tags=item["tags"],
            verification_status=VerificationStatus.VERIFIED,
            importance=0.8,
        )
        item["target_id"] = mid

    # 2. Populate distractors
    for dist in BENCHMARK_DISTRACTORS:
        store.remember(
            content=dist,
            repo_namespace="bench_repo",
            tier=MemoryTier.LEARNED,
            verification_status=VerificationStatus.UNVERIFIED,
            importance=0.3,
        )

    yield store
    con.close()


def test_retrieval_benchmark_precision_and_latency(populated_bench_store: ChronosMemoryStore):
    store = populated_bench_store
    queries = BENCHMARK_GROUND_TRUTH

    p1_hits = 0
    p3_hits = 0
    p5_hits = 0
    reciprocal_ranks = []
    latencies = []

    for item in queries:
        q = item["query"]
        target_id = item["target_id"]

        start_t = time.perf_counter()
        results = store.recall(query=q, repo_namespace="bench_repo", limit=5)
        elapsed_ms = (time.perf_counter() - start_t) * 1000.0
        latencies.append(elapsed_ms)

        retrieved_ids = [r.id for r in results]

        # Precision / Recall Rank Evaluation by Ground Truth ID
        if target_id in retrieved_ids:
            rank = retrieved_ids.index(target_id) + 1
            reciprocal_ranks.append(1.0 / rank)
            if rank == 1:
                p1_hits += 1
            if rank <= 3:
                p3_hits += 1
            if rank <= 5:
                p5_hits += 1
        else:
            reciprocal_ranks.append(0.0)

    num_queries = len(queries)
    p1 = p1_hits / num_queries
    p3 = p3_hits / num_queries
    p5 = p5_hits / num_queries
    mrr = sum(reciprocal_ranks) / num_queries
    avg_latency = sum(latencies) / num_queries

    print("\n--- Chronos Memory Retrieval Benchmark Report ---")
    print(f"Total Corpus Size: {num_queries + len(BENCHMARK_DISTRACTORS)} memories")
    print(f"Precision@1 : {p1 * 100:.1f}% ({p1_hits}/{num_queries})")
    print(f"Precision@3 : {p3 * 100:.1f}% ({p3_hits}/{num_queries})")
    print(f"Precision@5 : {p5 * 100:.1f}% ({p5_hits}/{num_queries})")
    print(f"MRR         : {mrr:.3f}")
    print(f"Avg Latency : {avg_latency:.2f} ms / query")

    # Rigorous benchmark thresholds
    assert p1 >= 0.75, f"Precision@1 ({p1}) must be >= 75%"
    assert p3 >= 0.85, f"Precision@3 ({p3}) must be >= 85%"
    assert p5 >= 0.90, f"Precision@5 ({p5}) must be >= 90%"
    assert mrr >= 0.80, f"MRR ({mrr}) must be >= 0.80"
    assert avg_latency < 5.0, f"Average retrieval latency ({avg_latency}ms) must be < 5ms"
