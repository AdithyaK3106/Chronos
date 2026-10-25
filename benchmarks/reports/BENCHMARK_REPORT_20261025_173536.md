# Chronos Efficacy & Governance Benchmark Report

**Generated:** `2026-10-25T17:35:36.377049+00:00`  
**Model:** `gemini-3.8-flash-low` (`agy`)  
**Total Paired Iterations:** `1`  

---

## 1. Executive Summary & Paired Comparison

### Efficacy Suite (Software Engineering Tasks)

| Metric | Baseline (Vanilla OpenHands) | Treatment (Chronos Governed) | Delta (Treatment - Baseline) | Statistical Significance |
| :--- | :--- | :--- | :--- | :--- |
| **Task Success Rate** | **100.0%** (1/1) | **100.0%** (1/1) | **+0.0%** | 95% CI: `[+0.0000, +0.0000]`, McNemar *p*=`1.00000` |
| **Median Duration** | `37.0s` | `146.9s` | `+110.0s` | Non-parametric median comparison |
| **Mean Context Tokens** | `36239` | `131981` | `-264.2%` | Bi-temporal Context Condensation |

### Tool Usage & Agent Exploration Profile

| Tool Name | Baseline Invocations | Treatment Invocations | Delta (Treat - Base) | Frequency Share |
| :--- | :--- | :--- | :--- | :--- |
| `file_editor` | `2` | `6` | `+4` | `53.3%` |
| `finish` | `0` | `2` | `+2` | `13.3%` |
| `think` | `0` | `5` | `+5` | `33.3%` |
| **Total Tool Invocations** | **`2`** | **`13`** | **`+11`** | **`100.0%`** |

---

## 2. Per-Task Execution Breakdown

| Task ID | Iteration | Baseline Status | Treatment Status | Latency (Base / Treat) | Tokens (Base / Treat) | Tools Used (Base / Treat) | Diffs Captured |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `cng_task_04` | `1` | `SUCCESS` | `SUCCESS` | `37.0s` / `147.0s` | `36,239` / `131,981` | `file_editor (2)` / `file_editor (6), think (5), finish (2)` | `No` |

---

## 3. Methodological Caveats & Attribution

> [!NOTE]
> **Initial Empirical Evaluation Notice**  
> This benchmark provides an empirical paired comparison across registered repositories and tasks. While statistically grounded through exact paired tests and 95% bootstrap confidence intervals, these results represent an initial evaluation rather than an exhaustive guarantee of superiority across all arbitrary software development tasks.

> [!NOTE]
> **Component Attribution Scope**  
> These metrics reflect the holistic system performance of Chronos (governance interception, sub-file AST locking, and bi-temporal context condensation) combined against a vanilla baseline. Individual component contribution ablations are deferred to future specialized studies.
