"""Configuration management for Chronos."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


from chronos.core.policy import POLICY_PRESETS  # noqa: E402 - re-exported


class PolicyConfig(BaseModel):
    """How enforcement decisions are presented to the developer.

    `preset` (relaxed / standard / strict) sets all fields at once; any field set explicitly in
    .chronos/config.json overrides the preset. Secrets are denied under every preset.
    """

    preset: Optional[Literal["relaxed", "standard", "strict"]] = Field(
        default=None, description="Named bundle of the settings below; explicit settings override it."
    )

    protected_modules: Literal["ask", "deny"] = Field(
        default="ask",
        description="Edits to protected modules (auth, policy, security, ...): ask the developer "
        "in the agent UI, or deny until `chronos gate grant`. Secrets are always denied.",
    )
    on_hook_error: Literal["ask", "deny", "allow"] = Field(
        default="ask",
        description="If the hook itself fails: ask (visible, not a lockout), deny (strict), or "
        "allow (silent fail-open, not recommended).",
    )
    outside_repo_writes: Literal["ask", "deny", "allow"] = Field(
        default="ask",
        description="Edits/writes to files outside the repository: ask the developer, deny, or "
        "allow (leave them to the agent's own permissions). Reads outside the repo are not checked.",
    )
    bash_backstop: bool = Field(
        default=True,
        description="After each Bash command, check whether protected or secret files changed without "
        "approval (needs git), and tell the agent and the audit log if so.",
    )
    max_context_chars: int = Field(
        default=6000, description="Upper bound on injected context, to limit prompt injection surface."
    )
    rules: dict[str, Literal["deny", "ask", "log", "off"]] = Field(
        default_factory=dict,
        description="Threat-rule overrides keyed by rule id (git.push_force), category (production) or '*'. "
        "See chronos.security.command_rules.",
    )

    @model_validator(mode="before")
    @classmethod
    def _apply_preset(cls, data):
        if isinstance(data, dict) and data.get("preset"):
            preset = POLICY_PRESETS.get(data["preset"])
            if preset is None:
                raise ValueError(f"Unknown policy preset {data['preset']!r}; choose from {', '.join(POLICY_PRESETS)}")
            rules = {**preset.get("rules", {}), **(data.get("rules") or {})}
            data = {**preset, **data, "rules": rules}
        return data


class SLMConfig(BaseModel):
    """Configuration for Zero-Path SLM-assisted Codebase Localization."""

    enabled: bool = Field(
        default=False,
        description="Whether SLM zero-path resolution is active. Default is False (opt-in).",
    )
    model: str = Field(
        default="OpenHands/CodeScout-1.7B",
        description="Target model identifier (fixed to OpenHands/CodeScout-1.7B).",
    )
    backend: str = Field(
        default="openai_compatible",
        description="Backend engine: 'openai_compatible', 'ollama', or 'agy'.",
    )
    endpoint: str = Field(
        default="http://localhost:8000/v1",
        description="Base URL for OpenAI-compatible or Ollama server.",
    )
    api_key: Optional[str] = Field(
        default="EMPTY",
        description="API key for authentication if required by proxy.",
    )
    timeout_seconds: int = Field(
        default=35,
        description="Maximum seconds before triggering circuit breaker and fallback.",
    )
    max_candidate_files: int = Field(
        default=150,
        description="Maximum candidate repository files provided in context to CodeScout.",
    )


class EmbeddingsConfig(BaseModel):
    """Optional meaning-based retriever (chronos/context/embeddings.py). Off by default."""

    enabled: bool = False
    model: str = "issue-loc-embed"
    endpoint: str = "http://127.0.0.1:11434"
    # "embeddings": the embedding ranking leads (best in artifacts/embed_eval_*: 17/30 top-3 on
    # held-out vs 13/30 for RRF). "rrf": reciprocal rank fusion with the lexical ranking.
    mode: Literal["embeddings", "rrf"] = "embeddings"
    top_files: int = Field(default=5, ge=1, le=20)
    timeout_seconds: float = Field(default=10.0, gt=0)


class RetrievalConfig(BaseModel):
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)


class ChronosConfig(BaseModel):
    repo_path: str = "."
    db_path: str = ".chronos/chronos.db"
    # None = the per-user data dir (chronos.core.paths.audit_log_path); a path here overrides it
    # (relative paths are relative to the repository).
    audit_log_path: Optional[str] = None
    rules_dir: str = ".chronos/rules"
    default_lock_ttl: int = 300
    graph_backend: str = "sqlite"  # 'sqlite' or 'kuzu'
    auth_mode: str = "strict"      # 'strict' or 'permissive'
    context_token_budget: int = 4000
    slm: SLMConfig = Field(default_factory=SLMConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)

    def audit_log_file(self, root: str | Path | None = None) -> Path:
        """Resolved audit log: the configured path, else the per-user location for this repo."""
        base = Path(root or self.repo_path).resolve()
        if self.audit_log_path:
            p = Path(self.audit_log_path)
            return p if p.is_absolute() else base / p
        from chronos.core.paths import audit_log_path

        return audit_log_path(base)

    @classmethod
    def load(cls, base_dir: str | Path | None = None) -> "ChronosConfig":
        root = Path(base_dir or os.environ.get("CHRONOS_REPO_PATH") or ".").resolve()
        cfg_file = root / ".chronos" / "config.json"
        
        cfg_dict: dict = {"repo_path": str(root)}
        if cfg_file.is_file():
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        cfg_dict.update(loaded)
            except Exception:
                pass
        
        # Override with environment variables if present
        if "CHRONOS_DB" in os.environ:
            cfg_dict["db_path"] = os.environ["CHRONOS_DB"]
        if "CHRONOS_AUTH" in os.environ:
            cfg_dict["auth_mode"] = os.environ["CHRONOS_AUTH"]
        if "CHRONOS_GRAPH_BACKEND" in os.environ:
            cfg_dict["graph_backend"] = os.environ["CHRONOS_GRAPH_BACKEND"]

        # SLM configuration overrides
        slm_dict = dict(cfg_dict.get("slm", {})) if isinstance(cfg_dict.get("slm"), dict) else {}
        if "CHRONOS_ENABLE_SLM" in os.environ:
            val = os.environ["CHRONOS_ENABLE_SLM"].strip().lower()
            slm_dict["enabled"] = val in ("1", "true", "yes", "on")
        if "CHRONOS_SLM_MODEL" in os.environ:
            slm_dict["model"] = os.environ["CHRONOS_SLM_MODEL"]
        if "CHRONOS_SLM_BACKEND" in os.environ:
            slm_dict["backend"] = os.environ["CHRONOS_SLM_BACKEND"]
        if "CHRONOS_SLM_ENDPOINT" in os.environ:
            slm_dict["endpoint"] = os.environ["CHRONOS_SLM_ENDPOINT"]
        if "CHRONOS_SLM_API_KEY" in os.environ:
            slm_dict["api_key"] = os.environ["CHRONOS_SLM_API_KEY"]
        if "CHRONOS_SLM_TIMEOUT" in os.environ:
            try:
                slm_dict["timeout_seconds"] = int(os.environ["CHRONOS_SLM_TIMEOUT"])
            except ValueError:
                pass
        if "CHRONOS_SLM_MAX_FILES" in os.environ:
            try:
                slm_dict["max_candidate_files"] = int(os.environ["CHRONOS_SLM_MAX_FILES"])
            except ValueError:
                pass

        if slm_dict:
            cfg_dict["slm"] = slm_dict

        return cls(**cfg_dict)
