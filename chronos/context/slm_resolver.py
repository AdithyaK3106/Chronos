"""Local SLM-powered Zero-Path intent & file resolver for Chronos.

Enables resolving exact repository files and symbols from natural-language task
prompts without explicit file paths or manual navigation, powered by OpenHands/CodeScout-1.7B.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from chronos.core.config import SLMConfig

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


class SLMCircuitBreaker:
    """Protects Chronos from repeated latency penalties when SLM backend is down."""

    def __init__(self, max_failures: int = 2) -> None:
        self.max_failures = max_failures
        self.consecutive_failures = 0
        self.is_open = False
        self.last_failure_reason: Optional[str] = None
        self.last_failure_time: float = 0.0

    def can_attempt(self) -> bool:
        """Check whether requests are allowed through to the backend."""
        return not self.is_open

    def record_success(self) -> None:
        """Reset failure counter after a successful response."""
        self.consecutive_failures = 0
        self.is_open = False
        self.last_failure_reason = None

    def record_failure(self, reason: str) -> None:
        """Record a failure and trip the circuit breaker if threshold is reached."""
        self.consecutive_failures += 1
        self.last_failure_reason = reason
        self.last_failure_time = time.time()
        if self.consecutive_failures >= self.max_failures:
            self.is_open = True
            logger.warning(
                f"[Chronos Circuit Breaker] CodeScout SLM backend tripped OPEN after "
                f"{self.consecutive_failures} failures ({reason}). Bypassing SLM for remainder of session."
            )

    def reset(self) -> None:
        """Force reset the breaker state."""
        self.consecutive_failures = 0
        self.is_open = False
        self.last_failure_reason = None


class LocalSLMResolver:
    """Zero-Path semantic resolver leveraging OpenHands/CodeScout-1.7B."""

    def __init__(
        self,
        repo_root: str | Path,
        config: Optional[SLMConfig] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        slm_endpoint: Optional[str] = None,
        backend: Optional[str] = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        
        # Build configuration with backward-compatible parameter overrides
        base_cfg = config or SLMConfig()
        cfg_dict = base_cfg.model_dump()
        if model is not None:
            cfg_dict["model"] = model
        if timeout_seconds is not None:
            cfg_dict["timeout_seconds"] = timeout_seconds
        if slm_endpoint is not None:
            cfg_dict["endpoint"] = slm_endpoint
        if backend is not None:
            cfg_dict["backend"] = backend

        self.config = SLMConfig(**cfg_dict)
        self.circuit_breaker = SLMCircuitBreaker(max_failures=2)

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def timeout_seconds(self) -> int:
        return self.config.timeout_seconds

    @property
    def slm_endpoint(self) -> str:
        return self.config.endpoint

    def list_candidate_files(self, max_files: Optional[int] = None) -> List[str]:
        """Collect source code files from repo root to provide targeted scope."""
        limit = max_files or self.config.max_candidate_files
        source_exts = {
            ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
            ".cpp", ".hpp", ".c", ".h",
        }
        ignore_dirs = {
            ".git", ".venv", "venv", "node_modules", "__pycache__", "build",
            "dist", ".mypy_cache", ".pytest_cache", ".chronos", "coverage",
            "runs", "diffs", "data", "reports", ".system_generated",
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
                        return sorted(files)[:limit]
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
                        if len(collected) >= limit:
                            break
                if len(collected) >= limit:
                    break
        except Exception:
            pass

        return sorted(collected)

    def resolve_prompt(
        self,
        prompt: str,
        repo_files: Optional[List[str]] = None,
    ) -> SLMResolutionResult:
        """Resolve suspected files and symbols using CodeScout-1.7B with circuit breaker and fallback."""
        if not prompt or not prompt.strip():
            return SLMResolutionResult()

        # Zero-Network Invariant: if SLM is disabled, do not open sockets or spawn processes
        if not self.config.enabled:
            return SLMResolutionResult()

        # Circuit breaker check
        if not self.circuit_breaker.can_attempt():
            logger.debug("[Chronos SLM] Circuit breaker is OPEN. Skipping SLM call.")
            return SLMResolutionResult()

        candidate_files = repo_files if repo_files is not None else self.list_candidate_files()
        files_text = "\n".join(f"- {f}" for f in candidate_files[: self.config.max_candidate_files])

        system_instruction = (
            "You are CodeScout, an expert code search and localization agent.\n"
            "Given a natural-language issue description and a list of repository source files,\n"
            "identify the exact target files and functions that must be modified to solve the problem.\n"
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

        user_content = f"REPOSITORY SOURCE FILES:\n{files_text}\n\nUSER TASK PROMPT:\n\"{prompt}\""

        # Route by configured backend
        backend_name = self.config.backend.lower()
        res_raw: Optional[str] = None

        try:
            if backend_name in ("openai_compatible", "openai", "vllm"):
                res_raw = self._call_openai_compatible(system_instruction, user_content)
            elif backend_name == "ollama":
                res_raw = self._call_ollama(system_instruction, user_content)
            elif backend_name == "agy":
                res_raw = self._call_agy(system_instruction, user_content)
            else:
                # Default fallback attempt: OpenAI-compatible then Agy
                try:
                    res_raw = self._call_openai_compatible(system_instruction, user_content)
                except Exception:
                    res_raw = self._call_agy(system_instruction, user_content)

            if res_raw:
                parsed = self._extract_json(res_raw)
                if parsed:
                    self.circuit_breaker.record_success()
                    raw_result = SLMResolutionResult.model_validate(parsed)
                    return self._filter_and_validate(raw_result, candidate_files)

        except Exception as exc:
            self.circuit_breaker.record_failure(str(exc))
            logger.debug(f"[Chronos SLM] Error during CodeScout inference: {exc}")

        return SLMResolutionResult()

    def _call_openai_compatible(self, system_instruction: str, user_content: str) -> Optional[str]:
        """Call standard OpenAI-compatible chat completion endpoint (vLLM, Ollama /v1, LiteLLM)."""
        base_endpoint = self.config.endpoint.rstrip("/")
        url = f"{base_endpoint}/chat/completions" if not base_endpoint.endswith("/chat/completions") else base_endpoint

        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Chronos-CodeScout/1.0",
        }
        if self.config.api_key and self.config.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8"))
                    choices = body.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "")
        except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError) as e:
            raise RuntimeError(f"OpenAI-compatible request to {url} failed: {e}") from e

        return None

    def _call_ollama(self, system_instruction: str, user_content: str) -> Optional[str]:
        """Call native Ollama HTTP endpoint (/api/chat)."""
        base_endpoint = self.config.endpoint.rstrip("/")
        url = f"{base_endpoint}/api/chat" if not base_endpoint.endswith("/api/chat") else base_endpoint

        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "options": {"temperature": 0.1},
        }

        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8"))
                    return body.get("message", {}).get("content", "")
        except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Ollama request to {url} failed: {e}") from e

        return None

    def _call_agy(self, system_instruction: str, user_content: str) -> Optional[str]:
        """Call local Antigravity CLI runner."""
        full_prompt = f"{system_instruction}\n\n{user_content}"
        agy_model = self.config.model if self.config.model.startswith("gemini") else "gemini-3.8-flash-low"
        cmd = [
            "agy",
            f"--model={agy_model}",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
            "--print",
            full_prompt,
        ]
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=self.config.timeout_seconds,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        raise RuntimeError(f"agy execution failed with exit code {proc.returncode}: {proc.stderr[:200]}")

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Safely parse JSON from raw LLM output, extracting from code fences if present."""
        text = text.strip()
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

    def _filter_and_validate(
        self, result: SLMResolutionResult, candidate_files: List[str]
    ) -> SLMResolutionResult:
        """Anti-Hallucination Gate: validate paths and reject ambiguous basename matches."""
        valid_files: List[str] = []

        for f in result.suspected_files:
            norm_f = Path(f).as_posix().lstrip("./")
            # 1. Exact match relative to repository root
            if (self.repo_root / norm_f).is_file():
                valid_files.append(norm_f)
                continue

            # 2. Basename matching with ambiguity rejection
            base_name = Path(norm_f).name
            matching_cands = [
                cand for cand in candidate_files
                if Path(cand).name == base_name or cand.endswith("/" + norm_f) or cand == norm_f
            ]

            if len(matching_cands) == 1:
                # Exactly one match: unambiguous resolution
                valid_files.append(matching_cands[0])
            elif len(matching_cands) > 1:
                # Ambiguous duplicate basenames (e.g. src/utils.py and tests/utils.py): REJECT
                logger.debug(
                    f"[Chronos Anti-Hallucination] Rejected ambiguous path '{norm_f}' "
                    f"matching multiple files: {matching_cands}"
                )
            else:
                # Zero matches: hallucinated path
                logger.debug(f"[Chronos Anti-Hallucination] Rejected non-existent path '{norm_f}'")

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
