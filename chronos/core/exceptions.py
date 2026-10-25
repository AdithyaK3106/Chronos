"""Domain-specific exceptions for Chronos."""

class ChronosError(Exception):
    """Base exception for all Chronos errors."""
    pass

class ChronosSecurityError(ChronosError):
    """Raised when an operation violates security boundaries or path policies."""
    pass

class PathTraversalError(ChronosSecurityError):
    """Raised when a path escapes repository boundaries or symlinks outside."""
    pass

class PolicyViolationError(ChronosSecurityError):
    """Raised when an action violates an active blocking security rule."""
    pass

class LockConflictError(ChronosError):
    """Raised when an AST node lock cannot be acquired due to a conflict."""
    def __init__(self, node_id: str, holder_id: str, intent: str, expires_at: str):
        super().__init__(f"Lock conflict on '{node_id}': held by '{holder_id}' for '{intent}' until {expires_at}")
        self.node_id = node_id
        self.holder_id = holder_id
        self.intent = intent
        self.expires_at = expires_at

class LockOwnershipError(ChronosError):
    """Raised when releasing or modifying a lock owned by another agent."""
    pass

class StorageError(ChronosError):
    """Raised on graph or ledger storage failures."""
    pass
