"""Configuration management for Chronos."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


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


class ChronosConfig(BaseModel):
    repo_path: str = "."
    db_path: str = ".chronos/chronos.db"
    audit_log_path: str = ".chronos/chronos-audit.log"
    rules_dir: str = ".chronos/rules"
    default_lock_ttl: int = 300
    graph_backend: str = "sqlite"  # 'sqlite' or 'kuzu'
    auth_mode: str = "strict"      # 'strict' or 'permissive'
    context_token_budget: int = 4000
    slm: SLMConfig = Field(default_factory=SLMConfig)

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
