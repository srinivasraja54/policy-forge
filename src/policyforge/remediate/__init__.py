"""Remediation: turning a detected violation into a candidate fix, and deciding
whether that fix is trustworthy -- both with zero model calls.

Two steps, deliberately separate:

  applier.py  Autofix + Finding -> FileChange. Mechanical, offline, pure.
  verify.py   Run the rule's `verify` command against the candidate, in a
              throwaway copy of the repo. The oracle; nothing is written to
              the real checkout unless this passes.

Opening a PR with the result is a later step (Phase 4's GitHub client) and
needs credentials this package does not.
"""

from __future__ import annotations

from .applier import FileChange, RemediationError, SkippedFix, plan_fix
from .verify import VerifyResult, verify_in_sandbox

__all__ = [
    "FileChange",
    "RemediationError",
    "SkippedFix",
    "plan_fix",
    "VerifyResult",
    "verify_in_sandbox",
]
