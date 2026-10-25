"""Core data models for Chronos."""

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class ASTNode(BaseModel):
    id: str
    name: str
    path: str
    kind: str  # Function, Class, Module, Method, Variable
    language: str = "python"
    docstring: str | None = None
    signature: str | None = None

class ASTEdge(BaseModel):
    src: str
    dst: str
    type: str  # CALLS, DEFINES, INHERITS, IMPORTS
    valid_at: str
    invalid_at: str | None = None

class ImportBinding(BaseModel):
    source_file: str
    source_module: str
    imported_symbol: str
    local_alias: str
    is_type_only: bool = False
    line_number: int = 1

class LockInfo(BaseModel):
    node_id: str
    agent_id: str
    session_id: str
    intent: str
    acquired_at: str = Field(default_factory=utc_now_iso)
    expires_at: str

class ProvenanceEvent(BaseModel):
    id: int | None = None
    node_id: str
    agent_id: str
    session_id: str
    action: str
    reason: str = ""
    timestamp: str = Field(default_factory=utc_now_iso)

class AuditEvent(BaseModel):
    event_type: str
    agent_id: str
    action: str
    target: str
    status: Literal["allow", "deny", "executed", "failed"]
    reason: str = ""
    timestamp: str = Field(default_factory=utc_now_iso)
    prev_hash: str = ""
    hash: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

class PermissionManifest(BaseModel):
    allowed_paths: list[str] = Field(default_factory=lambda: ["**"])
    denied_paths: list[str] = Field(default_factory=list)
    read_only: bool = False
    allowed_tools: list[str] = Field(default_factory=list)
    max_concurrent_locks: int = 10
    allow_emergency_locks: bool = False
