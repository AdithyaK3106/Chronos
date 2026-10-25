"""Statistical calculation engine for paired benchmark metrics."""

from __future__ import annotations

import math
import random
from typing import List, Optional, Tuple
import statistics

from benchmarks.harness.models import (
    PairedTrialResult,
    RunStatus,
    StatisticalMetrics,
    TaskSuiteType,
)


def mcnemar_exact_p_value(b_only: int, c_only: int) -> float:
    """Compute exact two-sided McNemar test p-value from discordant pairs."""
    n = b_only + c_only
    if n == 0:
        return 1.0

    k = min(b_only, c_only)
    # Exact binomial cumulative probability: P(X <= k) with p=0.5
    cum_prob = 0.0
    for i in range(k + 1):
        cum_prob += math.comb(n, i) * (0.5 ** n)
    p_val = min(1.0, 2.0 * cum_prob)
    return round(p_val, 5)


def paired_bootstrap_ci(
    pairs: List[Tuple[int, int]],
    n_iterations: int = 10000,
    ci_level: float = 0.95,
) -> Tuple[float, float]:
    """Compute 95% bootstrap confidence interval for paired difference (treatment - baseline)."""
    if not pairs:
        return 0.0, 0.0

    deltas = []
    n = len(pairs)
    random.seed(42)

    for _ in range(n_iterations):
        sample = random.choices(pairs, k=n)
        base_rate = sum(p[0] for p in sample) / n
        treat_rate = sum(p[1] for p in sample) / n
        deltas.append(treat_rate - base_rate)

    deltas.sort()
    lower_idx = int(((1.0 - ci_level) / 2.0) * n_iterations)
    upper_idx = int((1.0 - ((1.0 - ci_level) / 2.0)) * n_iterations)
    upper_idx = min(upper_idx, n_iterations - 1)

    return round(deltas[lower_idx], 4), round(deltas[upper_idx], 4)


def compute_metrics(
    trials: List[PairedTrialResult],
    suite: TaskSuiteType = TaskSuiteType.EFFICACY,
) -> StatisticalMetrics:
    """Calculate aggregated paired statistical metrics across trials."""
    valid_trials = [
        t for t in trials if t.baseline_run is not None and t.treatment_run is not None
    ]
    total_paired = len(valid_trials)
    unique_tasks = len(set(t.task_id for t in valid_trials))

    if total_paired == 0:
        return StatisticalMetrics(
            suite=suite,
            total_tasks=0,
            total_paired_runs=0,
            baseline_success_count=0,
            treatment_success_count=0,
            baseline_success_rate=0.0,
            treatment_success_rate=0.0,
            delta_success_rate=0.0,
            median_duration_baseline_sec=0.0,
            median_duration_treatment_sec=0.0,
            duration_delta_sec=0.0,
        )

    # Success determination
    if suite == TaskSuiteType.SECURITY:
        base_success = [
            1 if t.baseline_run.status == RunStatus.BLOCKED_PRE_EXECUTION else 0
            for t in valid_trials
        ]
        treat_success = [
            1 if t.treatment_run.status == RunStatus.BLOCKED_PRE_EXECUTION else 0
            for t in valid_trials
        ]
    else:
        base_success = [
            1 if t.baseline_run.status == RunStatus.SUCCESS else 0
            for t in valid_trials
        ]
        treat_success = [
            1 if t.treatment_run.status == RunStatus.SUCCESS else 0
            for t in valid_trials
        ]

    base_count = sum(base_success)
    treat_count = sum(treat_success)
    base_rate = base_count / total_paired
    treat_rate = treat_count / total_paired
    delta_rate = treat_rate - base_rate

    # McNemar discordant counts
    # n10: Base=1, Treat=0
    # n01: Base=0, Treat=1
    n10 = sum(1 for b, tr in zip(base_success, treat_success) if b == 1 and tr == 0)
    n01 = sum(1 for b, tr in zip(base_success, treat_success) if b == 0 and tr == 1)
    p_val = mcnemar_exact_p_value(n10, n01)

    # Paired Bootstrap CI
    pairs = list(zip(base_success, treat_success))
    ci_lower, ci_upper = paired_bootstrap_ci(pairs)

    # Durations
    base_durations = [t.baseline_run.duration_seconds for t in valid_trials]
    treat_durations = [t.treatment_run.duration_seconds for t in valid_trials]
    med_base_dur = statistics.median(base_durations) if base_durations else 0.0
    med_treat_dur = statistics.median(treat_durations) if treat_durations else 0.0

    # Tokens (where available)
    base_tokens = [
        t.baseline_run.usage.total_tokens
        for t in valid_trials
        if t.baseline_run.usage.total_tokens is not None
    ]
    treat_tokens = [
        t.treatment_run.usage.total_tokens
        for t in valid_trials
        if t.treatment_run.usage.total_tokens is not None
    ]
    mean_base_tok = statistics.mean(base_tokens) if base_tokens else None
    mean_treat_tok = statistics.mean(treat_tokens) if treat_tokens else None
    token_red = None
    if mean_base_tok and mean_treat_tok and mean_base_tok > 0:
        token_red = round(((mean_base_tok - mean_treat_tok) / mean_base_tok) * 100.0, 2)

    # Security specifics
    sec_attempted = total_paired if suite == TaskSuiteType.SECURITY else 0
    sec_blocked = sum(
        1 for t in valid_trials if t.treatment_run.status == RunStatus.BLOCKED_PRE_EXECUTION
    )
    sec_breach_base = sum(
        1 for t in valid_trials if t.baseline_run.status == RunStatus.SECURITY_BREACH
    )
    sec_breach_treat = sum(
        1 for t in valid_trials if t.treatment_run.status == RunStatus.SECURITY_BREACH
    )

    return StatisticalMetrics(
        suite=suite,
        total_tasks=unique_tasks,
        total_paired_runs=total_paired,
        baseline_success_count=base_count,
        treatment_success_count=treat_count,
        baseline_success_rate=round(base_rate, 4),
        treatment_success_rate=round(treat_rate, 4),
        delta_success_rate=round(delta_rate, 4),
        mcnemar_p_value=p_val,
        bootstrap_ci_95=[ci_lower, ci_upper],
        median_duration_baseline_sec=round(med_base_dur, 2),
        median_duration_treatment_sec=round(med_treat_dur, 2),
        duration_delta_sec=round(med_treat_dur - med_base_dur, 2),
        mean_token_usage_baseline=round(mean_base_tok, 1) if mean_base_tok else None,
        mean_token_usage_treatment=round(mean_treat_tok, 1) if mean_treat_tok else None,
        token_reduction_pct=token_red,
        security_attempted=sec_attempted,
        security_blocked_pre_execution=sec_blocked,
        security_breaches_baseline=sec_breach_base,
        security_breaches_treatment=sec_breach_treat,
    )
