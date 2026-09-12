"""Shared types for detectors.

A detector answers one question about one repo: does this rule match, and where?
Detectors never call a model. They are fast, deterministic, and cheap enough to
run on every push -- which is what makes prevention (rather than nightly
detection) affordable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Outcome(StrEnum):
    CLEAR = "clear"            # rule evaluated, no violation
    VIOLATING = "violating"    # rule evaluated, violation found
    ERROR = "error"            # rule could not be evaluated (tool/IO failure)


@dataclass(frozen=True)
class Finding:
    """One concrete violation, anchored to a file and line so it can become a diff."""

    file: str
    line: int
    excerpt: str
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "excerpt": self.excerpt,
            "detail": self.detail,
        }


@dataclass
class RuleResult:
    artifact_id: str
    rule_id: str
    repo: str
    outcome: Outcome
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.findings)


@dataclass
class RepoTarget:
    """A repo to evaluate. Local checkout today; a fetched tree later."""

    name: str
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
