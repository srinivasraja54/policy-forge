"""Remediation: turning a detected violation into a candidate fix, and deciding
whether that fix is trustworthy -- both with zero model calls.

Three steps:

  applier.py  Autofix + Finding -> FileChange. Mechanical, offline, pure.
  verify.py   Run the rule's `verify` command against the candidate, in a
              throwaway copy of the repo. The oracle; nothing is written to
              the real checkout unless this passes.
  github.py   Push a verified fix to a branch and open (or reuse) a PR for
              it. Needs `GITHUB_TOKEN` and the `github` extra (httpx) --
              deliberately *not* imported here, so the first two steps (what
              `policyctl remediate` does without `--pr`) stay dependency-free.
              Import it directly: `from policyforge.remediate.github import
              open_fix_pr`.
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
