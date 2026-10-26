# Production-Ready OpenHands CodeScout-1.7B SLM Integration Plan

**Status:** Audited & Ready for Implementation  
**Target Model:** `OpenHands/CodeScout-1.7B` (exclusively; no alternative/larger models)  
**Target Architecture:** Zero-Path Semantic Codebase Localization under Chronos Deterministic Governance  
**Hardware Environment Audited:** 
- GPU: NVIDIA GeForce GTX 750 Ti (2048 MiB VRAM, Maxwell compute capability 5.0)
- Host CPU: Intel(R) Core(TM) i3-4130 CPU @ 3.40GHz (Haswell, 2 cores / 4 threads, AVX2)
- System RAM: 7.2 GiB total (3.7 GiB available)
- NPU Status: **None detected** (`/sys/class/accel` empty, `/dev/accel*` absent)

---

## 1. Verified Model & Runtime Compatibility

### A. Model Architecture & Origin
- **Model:** `OpenHands/CodeScout-1.7B` (and `OpenHands/CodeScout-1.7B-RFT`).
- **Base Architecture:** Qwen-based 1.7B parameter transformer.
- **Training Strategy:** 2-stage training by the OpenHands team:
  1. Rejection Fine-Tuning (RFT) on expert exploration trajectories from larger models.
  2. Group Sequence Policy Optimization (GSPO) reinforcement learning on SWE-bench issues.
- **Target Task:** Repository-level fault & feature localization: mapping natural-language issue descriptions to the exact files, classes, and functions requiring modifications.

### B. Hardware & Serving Compatibility Matrix

| Runtime Backend | Model Format | VRAM Footprint | Host RAM | Supported on Local GTX 750 Ti (2GB) | Production Fit |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **vLLM** (`/v1/chat/completions`) | FP16 / BF16 | ~3.4 GB | ~4 GB | ❌ Exceeds 2GB VRAM | ✅ Ideal for Dedicated GPU Server |
| **vLLM** (AWQ / GPTQ 4-bit) | 4-bit quantized | ~1.3 GB | ~3 GB | ⚠️ Maxwell lacks INT4 tensor cores | ✅ Recommended for Ampere/Ada Server |
| **Ollama** (`/v1` or `/api/chat`) | Q4_K_M GGUF | ~1.1 GB | ~2.5 GB | ✅ Fits in 2GB VRAM or CPU RAM | ✅ Ideal for Local Dev Workstation |
| **llama-server** (OpenAI API) | Q4_K_M GGUF | ~1.1 GB | ~2.2 GB | ✅ CPU or GPU offload | ✅ Lightweight CLI / Embedded |
| **OpenAI-Compatible Remote Gateway** | Any remote | 0 MB (Client) | 0 MB (Client) | ✅ Fully Supported (Zero client GPU) | ✅ Recommended Enterprise Gateway |
| **Agy CLI Runner** | Cloud/CLI | 0 MB (Client) | 0 MB (Client) | ✅ Verified in Current Dev Environment | ✅ Development / Harness Fallback |

> [!NOTE]
> **NPU Compatibility Finding**  
> Inspection of the host system confirmed `/sys/class/accel` is empty and no NPU driver is loaded. While CodeScout-1.7B GGUF weights can run via OpenVINO or ONNX Runtime on Intel Core Ultra NPUs or Qualcomm Hexagon NPUs, **no NPU acceleration is claimed or enabled for this deployment without direct hardware presence**.

---

## 2. First-Release Backend Scope

To maintain a clean, lightweight, and robust architecture without bloat, the first release implements **three focused backends**:

1. **`openai_compatible` (Default Backend):**
   - Implements standard OpenAI chat completion API (`POST {endpoint}/chat/completions`).
   - Automatically supports: **vLLM**, **Ollama** (via its built-in `/v1` endpoint), **llama-server**, **LiteLLM proxy**, and **OpenAI-compatible gateways**.
   - Configurable timeout, connection pooling, and JSON schema extraction.
2. **`ollama` (Native Ollama API Backend):**
   - Connects to native Ollama HTTP endpoint (`POST {endpoint}/api/chat`).
   - Enables zero-config local developer execution when users run `ollama run hf.co/OpenHands/CodeScout-1.7B`.
3. **`agy` (Antigravity CLI Runner):**
   - Leverages local environment runner for regression and test harness compatibility.

*(Theoretical in-process PyTorch transformers backends are excluded from Phase 1 to prevent 3+ GB PyTorch dependencies from bloating non-GPU environments).*

---

## 3. Configuration Schema & Precedence Hierarchy

### A. Precedence Rule
SLM inference is **explicitly opt-in** and strictly **disabled by default**. The precedence order is:

$$\text{CLI Flags} > \text{Environment Variables} > \text{Repository Config (.chronos/config.json)} > \text{Defaults (enabled=False)}$$

### B. Configuration Schema (`chronos/core/config.py`)

```python
class SLMConfig(BaseModel):
    enabled: bool = Field(
        default=False, 
        description="Whether SLM zero-path resolution is active. When False, uses deterministic AST + BM25."
    )
    model: str = Field(
        default="OpenHands/CodeScout-1.7B",
        description="Target model identifier (fixed to OpenHands/CodeScout-1.7B)."
    )
    backend: str = Field(
        default="openai_compatible",
        description="Backend engine: 'openai_compatible', 'ollama', or 'agy'."
    )
    endpoint: str = Field(
        default="http://localhost:8000/v1",
        description="Endpoint URL for OpenAI-compatible or Ollama server."
    )
    api_key: Optional[str] = Field(
        default="EMPTY",
        description="API key or token for authenticated gateways."
    )
    timeout_seconds: int = Field(
        default=25,
        description="Maximum seconds before triggering circuit breaker and AST fallback."
    )
    max_candidate_files: int = Field(
        default=150,
        description="Maximum candidate repository files provided in context to CodeScout."
    )
```

---

## 4. Architectural Data Flow & Resolution Fusion

```mermaid
flowchart TD
    UserPrompt["Natural Language Prompt"] --> CheckSLM{"SLM Enabled?<br/>(CLI > ENV > config.json)"}
    
    CheckSLM -->|No / Default| DetEngine["Deterministic Engine<br/>Tree-Sitter AST + FTS5 BM25"]
    
    CheckSLM -->|Yes (Opt-In)| BoundedFiles["Collect Candidate Repo Files<br/>(Max N Files, Filtered Exts)"]
    BoundedFiles --> CBCheck{"Circuit Breaker<br/>Tripped?"}
    
    CBCheck -->|Yes (Unhealthy)| DetEngine
    CBCheck -->|No| CodeScout["Invoke CodeScout-1.7B<br/>(OpenAI-Compatible / Ollama / Agy)"]
    
    CodeScout -->|Timeout / Conn Error| TriggerCB["Trip Circuit Breaker & Log Warning"]
    TriggerCB --> DetEngine
    
    CodeScout -->|Raw Response| ParseJSON["Parse Structured Output<br/>(Files, Symbols, Rationale)"]
    ParseJSON --> ValidatePaths["Anti-Hallucination Gate:<br/>1. Check exact file exists<br/>2. Disambiguate unique basenames<br/>3. Reject ambiguous / missing paths<br/>4. Validate symbols against AST"]
    
    ValidatePaths --> ValidatedSLM["Validated SLM Candidates"]
    DetEngine --> DetCandidates["Deterministic Candidates"]
    
    ValidatedSLM --> Fusion["Graph & Lexical Fusion Reranker<br/>(Boost candidates confirmed by both)"]
    DetCandidates --> Fusion
    
    Fusion --> Budgeter["Chronos Token Budgeter"]
    Budgeter --> ContextEnvelope["Context Envelope (Pre-Turn-1 Injection)"]
    ContextEnvelope --> SecurityGates["Independent Security Gateway & AST Leases"]
```

---

## 5. Anti-Hallucination & Validation Rules

Model output is strictly untrusted advisory data:
1. **File Path Validation:**
   - Every file path returned by CodeScout must resolve to a valid, existing file under `repo_root`.
   - If CodeScout outputs an unqualified filename (e.g. `ledger.py`), the resolver searches candidate files:
     - If **exactly 1** candidate matches `Path(cand).name == "ledger.py"`, normalize to `chronos/coordination/ledger.py`.
     - If **multiple candidates** match (e.g. `src/utils.py` and `tests/utils.py`), **reject the suggestion as ambiguous** rather than making a random guess.
     - If **0 candidates** match, immediately discard.
2. **Symbol Validation:**
   - Check proposed symbols against indexed AST nodes in `SQLiteGraphStore`.
   - Unmatched symbols are demoted to plain search terms rather than authoritative target AST seeds.
3. **Advisory Immunity Guarantee:**
   - Retrieved SLM target files **never** grant write permissions, lease bypasses, or security approval. Leases must still be acquired through `chronos_acquire_lock` and actions verified against `security/permissions.py`.

---

## 6. Resilience, Timeouts & Circuit Breaker

- **Bounded Timeouts:** Every network call has an explicit `timeout_seconds` (default: 25s, configurable).
- **Circuit Breaker:**
  - Maintains consecutive failure count (`consecutive_failures`).
  - If 2 consecutive failures occur (connection refused, HTTP 500, read timeout), the circuit breaker trips into `OPEN` state for the remainder of the session.
  - While `OPEN`, subsequent prompt resolutions bypass the network call entirely and execute deterministic AST + BM25 instantly.
- **Zero-Network Invariant:**
  - When `enabled=False`, zero sockets are opened, zero background processes are spawned, and zero HTTP calls are dispatched.

---

## 7. CLI & Diagnostic Ergonomics

- `chronos resolve "<prompt>" [--enable-slm|--no-slm] [--slm-backend <b\>] [--slm-endpoint <url\>]`
- `chronos run "<prompt>" [--enable-slm|--no-slm] ...`
- `chronos doctor`:
  - Inspects SLM configuration status (Enabled / Disabled).
  - Pings the configured endpoint (HTTP health check).
  - Reports detected local accelerator hardware (GTX 750 Ti, RAM, NPU status).
  - Prints sanitized diagnostics without leaking API keys.

---

## 8. Measurable Acceptance Criteria & Localization Benchmark

1. **Configuration Precedence:**
   - `chronos resolve "..." --no-slm` executes pure AST + BM25 in < 50ms without network traffic.
   - `chronos resolve "..." --enable-slm` engages CodeScout.
2. **Resilience & Fallback:**
   - When pointing to dead endpoint `http://localhost:9999/v1`, resolver catches `ConnectionError`, trips circuit breaker, and returns deterministic AST results with zero unhandled exceptions.
3. **Anti-Hallucination:**
   - Hallucinated paths (e.g. `chronos/non_existent_module.py`) and ambiguous duplicate basenames are rejected 100% of the time.
4. **Security Independence:**
   - SLM resolution output cannot bypass path traversal, secret file blocks, or write permissions.
5. **Localization Benchmark:**
   - Measure File Recall@5, Recall@10, and Symbol Recall across enterprise tasks comparing:
     - Baseline: Deterministic AST + FTS5 BM25.
     - Treatment: Deterministic AST + CodeScout-1.7B + Graph Fusion.
6. **Test Suite:**
   - 100% green test pass rate (89+ tests).
