"""Structured output shapes for the two model stages.

These are deliberately *not* the `Artifact` type itself. The model proposes; our
code converts and validates. Keeping a conversion boundary between them means a
compilation that is well-formed JSON but wrong -- an id that collides, a detector
that does not fit the kind, a window on a policy -- is rejected by our schema
rather than written to the registry.

The shapes are also flatter than `Artifact`: optional sibling fields instead of
discriminated unions, because that is what strict JSON-schema mode handles
cleanly. The nesting is restored during conversion.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class TriageVerdict(BaseModel):
    """Stage 1 output. Cheap model, one call per message batch entry.

    The only job here is to decide whether a message is worth the expensive
    stage. Getting this wrong in the conservative direction costs a few cents;
    getting it wrong in the permissive direction costs a compile call per noise
    message, which is the entire cost argument for the funnel.
    """

    ref: str = Field(description="Echo of the message ref being judged.")
    actionable: bool = Field(
        description="True only if this imposes a checkable obligation on repos, "
        "pipelines or infrastructure."
    )
    kind: Literal["policy", "event", "amendment", "none"] = Field(
        description="policy = durable rule; event = time-bounded window; "
        "amendment = changes an obligation announced earlier; none = not actionable."
    )
    reason: str = Field(max_length=300, description="One sentence of justification.")
    confidence: float = Field(ge=0.0, le=1.0)


class ProposedDetect(BaseModel):
    type: Literal["file_pattern", "schedule_intersects"]
    globs: list[str] = Field(
        description="File globs to search, e.g. ['**/Dockerfile', '**/*.tf']."
    )
    regex: str | None = Field(
        default=None,
        description="Python-compatible regex. Required for file_pattern. Avoid "
        "PCRE2-only syntax; lookahead is supported.",
    )
    absent: bool = Field(
        default=False,
        description="True when the violation is the ABSENCE of the pattern.",
    )
    depends_on_regex: str | None = Field(
        default=None,
        description="For schedule_intersects: only flag jobs in files also "
        "matching this, so unrelated jobs are not false positives.",
    )


class ProposedAutofix(BaseModel):
    type: Literal["prefix_rewrite", "regex_replace", "shift_schedule", "manual"]
    to: str | None = None
    pattern: str | None = None
    replacement: str | None = None
    by_hours: int | None = None
    instructions: str | None = Field(
        default=None, description="Required when type is 'manual'."
    )


class ProposedRule(BaseModel):
    id: str = Field(description="Lowercase kebab-case, 2-63 chars.")
    description: str
    detect: ProposedDetect
    autofix: ProposedAutofix | None = None
    verify: str | None = Field(
        default=None,
        description="Shell command that must pass for a fix to be trusted. Set "
        "this whenever a mechanical check exists; leave null otherwise.",
    )


class ProposedArtifact(BaseModel):
    """Stage 2 output. Expensive model, one call per actionable message."""

    kind: Literal["policy", "event"]
    id: str = Field(description="Lowercase kebab-case slug, 3-79 chars, date-suffixed.")
    title: str = Field(max_length=200)
    severity: Literal["info", "warn", "high", "blocking"]
    rules: list[ProposedRule] = Field(
        min_length=1,
        description="One announcement often carries several distinct obligations. "
        "Emit one rule per obligation.",
    )
    deadline: date | None = Field(
        default=None, description="policy only: date the rule starts blocking."
    )
    window_start: datetime | None = Field(
        default=None, description="event only: UTC start of the window."
    )
    window_end: datetime | None = Field(default=None, description="event only: UTC end.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Your confidence that this compilation is faithful and the "
        "detection will not produce false positives. Be honest; low values route "
        "to closer human review rather than being discarded.",
    )
    reasoning: str = Field(
        max_length=1000,
        description="Brief note for the human reviewing the registry pull request.",
    )


class ProposedAmendment(BaseModel):
    """Stage 2 output when a message changes an obligation announced earlier.

    Amendments are diffs, not new artifacts. Treating them as new would leave two
    contradictory policies active; skipping them would let the registry go stale.
    """

    target_id: str = Field(description="Artifact id this amends.")
    change_summary: str = Field(max_length=500)
    new_deadline: date | None = None
    new_window_start: datetime | None = None
    new_window_end: datetime | None = None
    new_severity: Literal["info", "warn", "high", "blocking"] | None = None
    retire: bool = Field(
        default=False, description="True if the earlier obligation is withdrawn."
    )
    confidence: float = Field(ge=0.0, le=1.0)
