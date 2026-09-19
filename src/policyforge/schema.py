"""Artifact schema: the compiled, executable form of an org signal.

An artifact is what the compiler emits and what the enforcement engine runs.
It is stored in git (one JSON file per artifact) and reviewed as a PR before
it can act on any repository.

Two kinds, distinguished by how their truth behaves over time:

  policy  -- a durable predicate ("no unpinned base images"). True until retired.
  event   -- a time-bounded fact ("Artifactory is down Oct 4, 02:00-06:00 UTC").
             Self-expiring; enforcement is scoped to the window.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Kind(StrEnum):
    POLICY = "policy"
    EVENT = "event"


class Status(StrEnum):
    """Lifecycle. Only ACTIVE artifacts are enforced."""

    PROPOSED = "proposed"      # compiled, awaiting human review in the registry PR
    ACTIVE = "active"          # merged; enforced on every run
    SUPERSEDED = "superseded"  # replaced by a newer artifact (see superseded_by)
    RETIRED = "retired"        # deliberately switched off; kept for audit history


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    HIGH = "high"
    BLOCKING = "blocking"      # fails the enforce check as a required status


class SourceType(StrEnum):
    SLACK = "slack"
    POSTMORTEM = "postmortem"
    MAINTENANCE = "maintenance"
    MANUAL = "manual"


class Source(BaseModel):
    """Provenance. Every artifact must be traceable to the signal it came from."""

    model_config = ConfigDict(extra="forbid")

    type: SourceType
    ref: str = Field(description="Stable id of the origin, e.g. Slack channel+ts")
    permalink: str | None = None
    captured_at: datetime
    excerpt: str | None = Field(
        default=None,
        max_length=2000,
        description="The span of source text this artifact was compiled from.",
    )


class Amendment(BaseModel):
    """A change made to an artifact after it was first compiled.

    Recorded on the artifact, not only in the ledger. An artifact whose deadline
    no longer matches the excerpt it was compiled from is misleading read on its
    own, so whatever moved it has to travel with it.
    """

    model_config = ConfigDict(extra="forbid")

    source: Source
    summary: str = Field(max_length=500, description="What this change did.")
    applied_at: datetime
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class Window(BaseModel):
    """A maintenance / freeze window. Required for kind=event."""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> Window:
        if self.end <= self.start:
            raise ValueError("window.end must be after window.start")
        return self

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


# --------------------------------------------------------------------------
# Detectors -- the deterministic half. These run with zero model calls.
# --------------------------------------------------------------------------


class FilePatternDetect(BaseModel):
    """Match a regex against files selected by glob. Backed by ripgrep."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["file_pattern"] = "file_pattern"
    globs: list[str] = Field(min_length=1)
    regex: str
    # When True the rule is violated by the *absence* of a match in matched files,
    # which is how "every Dockerfile must declare a USER" style rules are expressed.
    absent: bool = False

    @field_validator("regex")
    @classmethod
    def _compilable(cls, v: str) -> str:
        import re

        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"regex does not compile: {exc}") from exc
        return v


class ScheduleIntersectsDetect(BaseModel):
    """Find scheduled jobs whose next firings land inside an event window.

    Parses cron expressions out of CI workflow and Kubernetes CronJob manifests
    and intersects them with the artifact's window. Pure calendar arithmetic --
    this is the detector that turns a maintenance notice into a concrete list of
    jobs that will fail.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["schedule_intersects"] = "schedule_intersects"
    globs: list[str] = Field(
        default_factory=lambda: [
            ".github/workflows/*.yml",
            ".github/workflows/*.yaml",
            "**/cronjob*.yaml",
            "**/cronjob*.yml",
        ]
    )
    # Optional extra gate: only flag jobs that also reference the affected system,
    # so a nightly job with no Artifactory dependency is not reported.
    depends_on_regex: str | None = None


Detect = Annotated[
    FilePatternDetect | ScheduleIntersectsDetect,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Remediation
# --------------------------------------------------------------------------


class PrefixRewrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["prefix_rewrite"] = "prefix_rewrite"
    to: str


class RegexReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["regex_replace"] = "regex_replace"
    pattern: str
    replacement: str


class ShiftSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["shift_schedule"] = "shift_schedule"
    by_hours: int
    revert_after: datetime | None = None


class ManualFix(BaseModel):
    """No safe mechanical fix -- open an issue describing what a human must do."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["manual"] = "manual"
    instructions: str


Autofix = Annotated[
    PrefixRewrite | RegexReplace | ShiftSchedule | ManualFix,
    Field(discriminator="type"),
]


class Rule(BaseModel):
    """One checkable obligation. A single announcement often compiles to several."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    description: str
    detect: Detect
    autofix: Autofix | None = None
    # Command that must pass for a proposed fix to be trusted. This is the oracle:
    # where it is set, a fix can be verified mechanically instead of by review.
    verify: str | None = None


class Artifact(BaseModel):
    """The compiled artifact. One per file under registry/."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Kind
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,78}$")
    title: str
    status: Status = Status.PROPOSED
    severity: Severity = Severity.WARN
    source: Source

    rules: list[Rule] = Field(min_length=1)

    # policy-only
    deadline: date | None = Field(
        default=None, description="Date the policy becomes blocking."
    )
    # event-only
    window: Window | None = None
    actions: list[str] = Field(default_factory=list)

    # Scoping: empty means "all repos in the catalog".
    include_repos: list[str] = Field(default_factory=list)
    exclude_repos: list[str] = Field(default_factory=list)

    superseded_by: str | None = None
    # Follow-ups that changed this artifact after it was first compiled, oldest
    # first. Appended to, never rewritten: the trail is the point.
    amendments: list[Amendment] = Field(default_factory=list)
    # Model's self-reported confidence in the compilation. Low values route to
    # human review even when auto-approval is otherwise enabled.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = None
    notes: str | None = None

    # ---- invariants -------------------------------------------------------

    @model_validator(mode="after")
    def _kind_fields_match(self) -> Artifact:
        if self.kind is Kind.EVENT:
            if self.window is None:
                raise ValueError("kind=event requires a window")
            if self.deadline is not None:
                raise ValueError("deadline is meaningless on kind=event; use window")
        else:
            if self.window is not None:
                raise ValueError("window is only valid on kind=event")
        if self.status is Status.SUPERSEDED and not self.superseded_by:
            raise ValueError("status=superseded requires superseded_by")
        dupes = {r.id for r in self.rules}
        if len(dupes) != len(self.rules):
            raise ValueError("rule ids must be unique within an artifact")
        return self

    # ---- enforcement gating ----------------------------------------------

    def is_enforceable(self, now: datetime | None = None) -> bool:
        """Only ACTIVE artifacts enforce, and events only inside their window."""
        if self.status is not Status.ACTIVE:
            return False
        if self.kind is Kind.EVENT:
            assert self.window is not None
            now = now or datetime.now(timezone.utc)
            # Events remain enforceable from compile time until the window closes,
            # so pre-flight warnings fire before the window, not only during it.
            return now < self.window.end
        return True

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.kind is not Kind.EVENT or self.window is None:
            return False
        return (now or datetime.now(timezone.utc)) >= self.window.end

    def applies_to(self, repo: str) -> bool:
        if repo in self.exclude_repos:
            return False
        return not self.include_repos or repo in self.include_repos

    def is_blocking(self, now: datetime | None = None) -> bool:
        """BLOCKING severity fails CI -- but a policy with a future deadline
        only starts blocking once that deadline arrives."""
        if self.severity is not Severity.BLOCKING:
            return False
        if self.kind is Kind.POLICY and self.deadline is not None:
            today = (now or datetime.now(timezone.utc)).date()
            return today >= self.deadline
        return True
