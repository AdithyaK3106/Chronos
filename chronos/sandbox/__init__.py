"""`chronos exec`: run an agent inside an OS sandbox derived from Chronos policy.

Hooks only see tool calls; a shell command (`sed -i`, `>`, `mv`, ...) never reaches them. The
sandbox enforces the same policy at the operating-system level, for every process the agent starts.

Rules (each one was required by the 3 Nov spike):
- Protected modules are bind-mounted read-only.
- Every ancestor directory of a protected file, up to and including the repository root, is bound
  onto itself. Mount points cannot be renamed, so `mv parent parent2` can no longer move the real
  file away and let a new file be written at the protected path.
- Secret files are made unreadable (a device node is bound over them), so even `cat .env` fails.
- Chronos's own policy and hook configuration is read-only, so the agent cannot disable its guardrails.

Only protected files are guaranteed. The rest of the repository stays writable, so deleting or
editing unprotected files is still possible.
"""

from chronos.sandbox.plan import SandboxPlan, plan_sandbox
from chronos.sandbox.runner import SandboxUnavailable, build_command, run

__all__ = ["SandboxPlan", "plan_sandbox", "SandboxUnavailable", "build_command", "run"]
