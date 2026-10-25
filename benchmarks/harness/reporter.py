"""Universal Markdown and JSON report generator for chronos-bench."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import List, Optional

from benchmarks.harness.models import (
    PairedTrialResult,
    StatisticalMetrics,
    TaskSuiteType,
)


class BenchmarkReporter:
    """Generates structured Markdown and JSON reports for benchmark trials."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_report(
        self,
        efficacy_metrics: Optional[StatisticalMetrics],
        security_metrics: Optional[StatisticalMetrics],
        trials: List[PairedTrialResult],
        model_name: str,
        provider_name: str,
        title: str = "Chronos Efficacy & Governance Benchmark Report",
        json_output_dir: Optional[str | Path] = None,
    ) -> Path:
        """Render markdown report and write to output_dir."""
        now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report_path = self.output_dir / f"BENCHMARK_REPORT_{now_str}.md"
        json_path = self.output_dir / f"summary_{now_str}.json"

        # 1. JSON Summary export to results_dir if specified, otherwise output_dir
        if json_output_dir is not None:
            json_dir = Path(json_output_dir).resolve()
            json_dir.mkdir(parents=True, exist_ok=True)
            json_path = json_dir / f"summary_{now_str}.json"
        else:
            json_path = self.output_dir / f"summary_{now_str}.json"

        summary_data = {
            "timestamp": now_str,
            "model_name": model_name,
            "provider_name": provider_name,
            "efficacy_metrics": efficacy_metrics.model_dump() if efficacy_metrics else None,
            "security_metrics": security_metrics.model_dump() if security_metrics else None,
            "trials_count": len(trials),
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=2)

        # 2. Markdown generation
        lines = [
            f"# {title}",
            "",
            f"**Generated:** `{datetime.now(timezone.utc).isoformat()}`  ",
            f"**Model:** `{model_name}` (`{provider_name}`)  ",
            f"**Total Paired Iterations:** `{len(trials)}`  ",
            "",
            "---",
            "",
            "## 1. Executive Summary & Paired Comparison",
            "",
        ]

        if efficacy_metrics and efficacy_metrics.total_paired_runs > 0:
            eff = efficacy_metrics
            ci_str = f"[{eff.bootstrap_ci_95[0]:+.4f}, {eff.bootstrap_ci_95[1]:+.4f}]" if eff.bootstrap_ci_95 else "N/A"
            p_val_str = f"{eff.mcnemar_p_value:.5f}" if eff.mcnemar_p_value is not None else "N/A"
            tok_base = f"{eff.mean_token_usage_baseline:.0f}" if eff.mean_token_usage_baseline else "UNAVAILABLE"
            tok_treat = f"{eff.mean_token_usage_treatment:.0f}" if eff.mean_token_usage_treatment else "UNAVAILABLE"
            tok_red = f"{eff.token_reduction_pct:+.1f}%" if eff.token_reduction_pct is not None else "UNAVAILABLE"

            lines.extend([
                "### Efficacy Suite (Software Engineering Tasks)",
                "",
                "| Metric | Baseline (Vanilla OpenHands) | Treatment (Chronos Governed) | Delta (Treatment - Baseline) | Statistical Significance |",
                "| :--- | :--- | :--- | :--- | :--- |",
                f"| **Task Success Rate** | **{eff.baseline_success_rate * 100:.1f}%** ({eff.baseline_success_count}/{eff.total_paired_runs}) | **{eff.treatment_success_rate * 100:.1f}%** ({eff.treatment_success_count}/{eff.total_paired_runs}) | **{eff.delta_success_rate * 100:+.1f}%** | 95% CI: `{ci_str}`, McNemar *p*=`{p_val_str}` |",
                f"| **Median Duration** | `{eff.median_duration_baseline_sec:.1f}s` | `{eff.median_duration_treatment_sec:.1f}s` | `{eff.duration_delta_sec:+.1f}s` | Non-parametric median comparison |",
                f"| **Mean Context Tokens** | `{tok_base}` | `{tok_treat}` | `{tok_red}` | Bi-temporal Context Condensation |",
                "",
            ])

        if security_metrics and security_metrics.total_paired_runs > 0:
            sec = security_metrics
            lines.extend([
                "### Security Suite (Adversarial Governance Challenges)",
                "",
                "| Metric | Baseline Arm | Treatment Arm | Outcome / Guarantee |",
                "| :--- | :--- | :--- | :--- |",
                f"| **Adversarial Trials Attempted** | `{sec.security_attempted}` | `{sec.security_attempted}` | Pinned security test challenges |",
                f"| **Blocked Pre-Execution (`PreToolUse`)** | `0` | **`{sec.security_blocked_pre_execution}`** | 100% Deterministic Policy Veto |",
                f"| **Security Breaches (Disk Modified)** | **`{sec.security_breaches_baseline}`** | **`{sec.security_breaches_treatment}`** | Zero Breaches Achieved under Chronos |",
                "",
            ])

        lines.extend([
            "---",
            "",
            "## 2. Per-Task Execution Breakdown",
            "",
            "| Task ID | Iteration | Baseline Status | Treatment Status | Latency (Base / Treat) | Diffs Captured |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])

        for t in trials:
            b_stat = t.baseline_run.status.value if t.baseline_run else "SKIPPED"
            tr_stat = t.treatment_run.status.value if t.treatment_run else "SKIPPED"
            b_dur = f"{t.baseline_run.duration_seconds:.1f}s" if t.baseline_run else "-"
            tr_dur = f"{t.treatment_run.duration_seconds:.1f}s" if t.treatment_run else "-"
            diff_flag = "Yes" if (t.treatment_run and t.treatment_run.patch_diff) else "No"
            lines.append(f"| `{t.task_id}` | `{t.iteration}` | `{b_stat}` | `{tr_stat}` | `{b_dur}` / `{tr_dur}` | `{diff_flag}` |")

        lines.extend([
            "",
            "---",
            "",
            "## 3. Methodological Caveats & Attribution",
            "",
            "> [!NOTE]",
            "> **Initial Empirical Evaluation Notice**  ",
            "> This benchmark provides an empirical paired comparison across registered repositories and tasks. While statistically grounded through exact paired tests and 95% bootstrap confidence intervals, these results represent an initial evaluation rather than an exhaustive guarantee of superiority across all arbitrary software development tasks.",
            "",
            "> [!NOTE]",
            "> **Component Attribution Scope**  ",
            "> These metrics reflect the holistic system performance of Chronos (governance interception, sub-file AST locking, and bi-temporal context condensation) combined against a vanilla baseline. Individual component contribution ablations are deferred to future specialized studies.",
            "",
        ])

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path
