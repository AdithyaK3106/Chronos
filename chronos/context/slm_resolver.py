"""Local SLM-powered Zero-Path intent & file resolver for Chronos.

Enables resolving exact repository files and symbols from natural-language task
prompts without explicit file paths or manual navigation.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SLMResolutionResult(BaseModel):
    """Structured semantic resolution from natural-language task prompt."""

    suspected_files: List[str] = Field(
        default_factory=list, description="Exact file paths relevant to the prompt"
    )
    suspected_symbols: List[str] = Field(
        default_factory=list, description="Class, function, or method names relevant to the task"
    )
    domain_concepts: List[str] = Field(
        default_factory=list, description="Key domain concepts extracted from the problem"
    )
    intent: str = Field(
        default="bug_fix", description="Task intent (bug_fix, feature, refactor, security)"
    )
    rationale: str = Field(
        default="", description="Brief explanation for file/symbol selection"
    )


class LocalSLMResolver:
    """Zero-Path semantic resolver leveraging local SLM inference."""

    def __init__(
        self,
        repo_root: str | Path,
        model: str = "gemini-3.8-flash-low",
        timeout_seconds: int = 25,
        slm_endpoint: Optional[str] = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.slm_endpoint = slm_endpoint or os.environ.get("CHRONOS_SLM_ENDPOINT")

    def list_candidate_files(self, max_files: int = 150) -> List[str]:
        """Collect source files from repo root to provide targeted scope."""
        source_exts = {
            ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
            ".cpp", ".hpp", ".c", ".h", ".sql", ".yaml", ".yml", ".json",
        }
        ignore_dirs = {
            ".git", ".venv", "venv", "node_modules", "__pycache__", "build",
            "dist", ".mypy_cache", ".pytest_cache", ".chronos", "coverage",
        }

        # 1. Try git ls-files if repo is a git repository
        if (self.repo_root / ".git").exists():
            try:
                cmd = ["git", "-C", str(self.repo_root), "ls-files"]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if proc.returncode == 0 and proc.stdout.strip():
                    files = [
                        line.strip()
                        for line in proc.stdout.splitlines()
                        if line.strip()
                        and Path(line.strip()).suffix.lower() in source_exts
                        and not any(ig in line for ig in ignore_dirs)
                    ]
                    if files:
                        return sorted(files)[:max_files]
            except Exception:
                pass

        # 2. Filesystem walk fallback
        collected: List[str] = []
        try:
            for root, dirs, filenames in os.walk(str(self.repo_root)):
                # Prune ignored directories in-place
                dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
                rel_root = Path(root).relative_to(self.repo_root)
                for f in filenames:
                    p = Path(f)
                    if p.suffix.lower() in source_exts:
                        rel_file = (rel_root / f).as_posix()
                        collected.append(rel_file)
                        if len(collected) >= max_files:
                            break
                if len(collected) >= max_files:
                    break
        except Exception:
            pass

        return sorted(collected)

    def resolve_prompt(
        self,
        prompt: str,
        repo_files: Optional[List[str]] = None,
    ) -> SLMResolutionResult:
        """Resolve suspected files and symbols from natural-language prompt."""
        if not prompt or not prompt.strip():
            return SLMResolutionResult()

        files = repo_files if repo_files is not None else self.list_candidate_files()
        files_text = "\n".join(f"- {f}" for f in files[:120])

        system_instruction = (
            "You are Chronos SLM Zero-Path Codebase Resolver.\n"
            "Given the user's task prompt written in natural language (without explicit file paths),\n"
            "analyze the problem and determine which repository file(s) and symbol(s) are relevant.\n"
            "Output ONLY a raw JSON object matching this schema:\n"
            "{\n"
            '  "suspected_files": ["relative/path/to/file.py"],\n'
            '  "suspected_symbols": ["symbol_name_or_class"],\n'
            '  "domain_concepts": ["concept1", "concept2"],\n'
            '  "intent": "bug_fix" | "feature" | "refactor" | "security",\n'
            '  "rationale": "One concise sentence explaining why these files were selected"\n'
            "}\n"
            "Do not include any markdown fences or explanatory text outside the JSON."
        )

        user_content = f"REPOSITORY FILES:\n{files_text}\n\nUSER TASK PROMPT:\n\"{prompt}\""

        # 1. Try local OpenAI-compatible endpoint if configured
        if self.slm_endpoint:
            try:
                res = self._call_endpoint(system_instruction, user_content)
                if res:
                    return self._filter_and_validate(res)
            except Exception as e:
                logger.debug(f"SLM HTTP endpoint call failed: {e}")

        # 2. Try agy CLI (local Antigravity SLM runner)
        try:
            full_prompt = f"{system_instruction}\n\n{user_content}"
            cmd = [
                "agy",
                f"--model={self.model}",
                "--disable-slash-commands",
                "--dangerously-skip-permissions",
                "--print",
                full_prompt,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parsed = self._extract_json(proc.stdout)
                if parsed:
                    return self._filter_and_validate(SLMResolutionResult.model_validate(parsed))
        except Exception as e:
            logger.debug(f"agy SLM call failed: {e}")

        # 3. Resilient fallback: empty resolution
        return SLMResolutionResult()

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Safely parse JSON from raw LLM output, extracting from code fences if present."""
        text = text.strip()
        # Remove markdown code blocks if wrapped
        if "```json" in text:
            match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()
        elif "```" in text:
            match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        # Extract outermost JSON object
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
        return None

    def _filter_and_validate(self, result: SLMResolutionResult) -> SLMResolutionResult:
        """Filter out hallucinatory file paths that do not exist in the repository."""
        valid_files: List[str] = []
        for f in result.suspected_files:
            norm_f = Path(f).as_posix()
            if (self.repo_root / norm_f).is_file():
                valid_files.append(norm_f)
            else:
                # Check if matches any existing file basename or relative suffix
                for cand in self.list_candidate_files():
                    if cand.endswith(norm_f) or Path(cand).name == Path(norm_f).name:
                        valid_files.append(cand)
                        break

        # Deduplicate while preserving order
        deduped_files: List[str] = []
        for vf in valid_files:
            if vf not in deduped_files:
                deduped_files.append(vf)

        return SLMResolutionResult(
            suspected_files=deduped_files,
            suspected_symbols=result.suspected_symbols,
            domain_concepts=result.domain_concepts,
            intent=result.intent,
            rationale=result.rationale,
        )
