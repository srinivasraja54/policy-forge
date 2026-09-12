"""Detector dispatch.

`evaluate` is the single entry point the enforcement engine uses. It routes a
rule to the right detector by the discriminated `detect.type`, so adding a new
detector (dependency graph, Terraform plan, cloud API) means adding a branch
here and a variant to the schema union -- nothing else changes.
"""

from __future__ import annotations

from datetime import datetime

from ..schema import Artifact, FilePatternDetect, Rule, ScheduleIntersectsDetect
from . import file_pattern, schedule
from .base import Finding, Outcome, RepoTarget, RuleResult

__all__ = ["evaluate", "evaluate_artifact", "Finding", "Outcome", "RepoTarget", "RuleResult"]


def evaluate(artifact: Artifact, rule: Rule, target: RepoTarget) -> RuleResult:
    d = rule.detect
    if isinstance(d, FilePatternDetect):
        return file_pattern.run(
            d, target, artifact_id=artifact.id, rule_id=rule.id
        )
    if isinstance(d, ScheduleIntersectsDetect):
        if artifact.window is None:
            return RuleResult(
                artifact_id=artifact.id,
                rule_id=rule.id,
                repo=target.name,
                outcome=Outcome.ERROR,
                error="schedule_intersects requires an artifact window (kind=event)",
            )
        return schedule.run(
            d, target, artifact.window, artifact_id=artifact.id, rule_id=rule.id
        )
    return RuleResult(
        artifact_id=artifact.id,
        rule_id=rule.id,
        repo=target.name,
        outcome=Outcome.ERROR,
        error=f"no detector registered for type {getattr(d, 'type', d)!r}",
    )


def evaluate_artifact(
    artifact: Artifact, target: RepoTarget, now: datetime | None = None
) -> list[RuleResult]:
    """Evaluate every rule of an artifact against one repo.

    Scoping (include/exclude) is applied here so callers cannot forget it.
    """
    if not artifact.applies_to(target.name):
        return []
    return [evaluate(artifact, rule, target) for rule in artifact.rules]
