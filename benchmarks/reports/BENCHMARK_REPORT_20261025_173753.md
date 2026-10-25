# Chronos Efficacy & Governance Benchmark Report

**Generated:** `2026-10-25T17:37:53.312200+00:00`  
**Model:** `gemini-3.8-flash-low` (`agy`)  
**Total Paired Iterations:** `1`  

---

## 1. Executive Summary & Paired Comparison

### Efficacy Suite (Software Engineering Tasks)

| Metric | Baseline (Vanilla OpenHands) | Treatment (Chronos Governed) | Delta (Treatment - Baseline) | Statistical Significance |
| :--- | :--- | :--- | :--- | :--- |
| **Task Success Rate** | **100.0%** (1/1) | **100.0%** (1/1) | **+0.0%** | 95% CI: `[+0.0000, +0.0000]`, McNemar *p*=`1.00000` |
| **Median Duration** | `78.4s` | `28.8s` | `-49.6s` | Non-parametric median comparison |
| **Mean Context Tokens** | `72792` | `19741` | `+72.9%` | Bi-temporal Context Condensation |

### Tool Usage & Agent Exploration Profile

| Tool Name | Baseline Invocations | Treatment Invocations | Delta (Treat - Base) | Frequency Share |
| :--- | :--- | :--- | :--- | :--- |
| `file_editor` | `4` | `1` | `-3` | `71.4%` |
| `finish` | `2` | `0` | `-2` | `28.6%` |
| **Total Tool Invocations** | **`6`** | **`1`** | **`-5`** | **`100.0%`** |

---

## 2. Per-Task Execution Breakdown

| Task ID | Iteration | Baseline Status | Treatment Status | Latency (Base / Treat) | Tokens (Base / Treat) | Tools Used (Base / Treat) | Diffs Captured |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `cng_task_05` | `1` | `SUCCESS` | `SUCCESS` | `78.4s` / `28.8s` | `72,792` / `19,741` | `file_editor (4), finish (2)` / `file_editor (1)` | `No` |

---

## 3. Methodological Caveats & Attribution

> [!NOTE]
> **Initial Empirical Evaluation Notice**  
> This benchmark provides an empirical paired comparison across registered repositories and tasks. While statistically grounded through exact paired tests and 95% bootstrap confidence intervals, these results represent an initial evaluation rather than an exhaustive guarantee of superiority across all arbitrary software development tasks.

> [!NOTE]
> **Component Attribution Scope**  
> These metrics reflect the holistic system performance of Chronos (governance interception, sub-file AST locking, and bi-temporal context condensation) combined against a vanilla baseline. Individual component contribution ablations are deferred to future specialized studies.
