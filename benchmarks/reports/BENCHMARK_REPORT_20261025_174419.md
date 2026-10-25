# Chronos Efficacy & Governance Benchmark Report

**Generated:** `2026-10-25T17:44:19.417771+00:00`  
**Model:** `gemini-3.8-flash-low` (`agy`)  
**Total Paired Iterations:** `1`  

---

## 1. Executive Summary & Paired Comparison

### Efficacy Suite (Software Engineering Tasks)

| Metric | Baseline (Vanilla OpenHands) | Treatment (Chronos Governed) | Delta (Treatment - Baseline) | Statistical Significance |
| :--- | :--- | :--- | :--- | :--- |
| **Task Success Rate** | **0.0%** (0/1) | **0.0%** (0/1) | **+0.0%** | 95% CI: `[+0.0000, +0.0000]`, McNemar *p*=`1.00000` |
| **Median Duration** | `180.0s` | `180.0s` | `+0.0s` | Non-parametric median comparison |
| **Mean Context Tokens** | `UNAVAILABLE` | `UNAVAILABLE` | `UNAVAILABLE` | Bi-temporal Context Condensation |

---

## 2. Per-Task Execution Breakdown

| Task ID | Iteration | Baseline Status | Treatment Status | Latency (Base / Treat) | Tokens (Base / Treat) | Tools Used (Base / Treat) | Diffs Captured |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `cooling_task_02` | `1` | `TIMEOUT` | `TIMEOUT` | `180.0s` / `180.0s` | `N/A` / `N/A` | `None` / `None` | `No` |

---

## 3. Methodological Caveats & Attribution

> [!NOTE]
> **Initial Empirical Evaluation Notice**  
> This benchmark provides an empirical paired comparison across registered repositories and tasks. While statistically grounded through exact paired tests and 95% bootstrap confidence intervals, these results represent an initial evaluation rather than an exhaustive guarantee of superiority across all arbitrary software development tasks.

> [!NOTE]
> **Component Attribution Scope**  
> These metrics reflect the holistic system performance of Chronos (governance interception, sub-file AST locking, and bi-temporal context condensation) combined against a vanilla baseline. Individual component contribution ablations are deferred to future specialized studies.
