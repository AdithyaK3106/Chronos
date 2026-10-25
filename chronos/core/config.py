"""Configuration management for Chronos."""

import json
import os
from pathlib import Path
from pydantic import BaseModel, Field

class ChronosConfig(BaseModel):
    repo_path: str = "."
    db_path: str = ".chronos/chronos.db"
    audit_log_path: str = ".chronos/chronos-audit.log"
    rules_dir: str = ".chronos/rules"
    default_lock_ttl: int = 300
    graph_backend: str = "sqlite"  # 'sqlite' or 'kuzu'
    auth_mode: str = "strict"      # 'strict' or 'permissive'
    context_token_budget: int = 4000

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

        return cls(**cfg_dict)
