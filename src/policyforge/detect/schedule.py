"""Cron-intersection detection for maintenance / freeze windows.

Extracts cron expressions from CI workflows and Kubernetes CronJobs, then
computes which of them fire inside an event window. This is pure calendar
arithmetic -- no model involved -- and it is what converts "Artifactory is down
Saturday 02:00-06:00 UTC" into "these three jobs will fail".

Timezone handling: GitHub Actions cron is always UTC. Kubernetes CronJob is UTC
unless spec.timeZone says otherwise, which we honour when present.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import CroniterBadCronError, croniter

from ..schema import ScheduleIntersectsDetect, Window
from .base import Finding, Outcome, RepoTarget, RuleResult
from .file_pattern import _iter_glob_files

# Cap the firings we enumerate so a pathological expression (e.g. "* * * * *")
# against a long window cannot spin.
_MAX_FIRINGS = 500


def run(
    detect: ScheduleIntersectsDetect,
    target: RepoTarget,
    window: Window,
    *,
    artifact_id: str,
    rule_id: str,
) -> RuleResult:
    def result(outcome: Outcome, findings=None, error=None) -> RuleResult:
        return RuleResult(
            artifact_id=artifact_id,
            rule_id=rule_id,
            repo=target.name,
            outcome=outcome,
            findings=findings or [],
            error=error,
        )

    if not target.path.exists():
        return result(Outcome.ERROR, error=f"path does not exist: {target.path}")

    dep_re = re.compile(detect.depends_on_regex) if detect.depends_on_regex else None
    findings: list[Finding] = []
    errors: list[str] = []

    for rel in _iter_glob_files(detect.globs, target.path):
        try:
            raw = (target.path / rel).read_text(errors="replace")
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
            continue

        # The dependency gate is evaluated per file: a nightly job in a repo that
        # never touches the affected system should not be reported.
        if dep_re and not dep_re.search(raw):
            continue

        for schedule in _extract_schedules(raw, str(rel)):
            hits = _firings_in_window(schedule.expr, window, schedule.tz)
            if not hits:
                continue
            shown = ", ".join(h.strftime("%Y-%m-%d %H:%M %Z") for h in hits[:3])
            more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
            findings.append(
                Finding(
                    file=schedule.file,
                    line=schedule.line,
                    excerpt=schedule.expr,
                    detail=(
                        f"{schedule.job or 'scheduled job'} fires in window: "
                        f"{shown}{more}"
                    ),
                )
            )

    if errors and not findings:
        return result(Outcome.ERROR, error="; ".join(errors[:5]))
    return result(Outcome.VIOLATING if findings else Outcome.CLEAR, findings)


class _Schedule:
    __slots__ = ("expr", "file", "line", "job", "tz")

    def __init__(self, expr: str, file: str, line: int, job: str | None, tz: str | None):
        self.expr, self.file, self.line, self.job, self.tz = expr, file, line, job, tz


def _extract_schedules(raw: str, rel: str) -> list[_Schedule]:
    try:
        doc_iter = list(yaml.safe_load_all(raw))
    except yaml.YAMLError:
        return []

    out: list[_Schedule] = []
    for doc in doc_iter:
        if not isinstance(doc, dict):
            continue
        out.extend(_from_github_workflow(doc, rel, raw))
        out.extend(_from_k8s_cronjob(doc, rel, raw))
    return out


def _from_github_workflow(doc: dict, rel: str, raw: str) -> list[_Schedule]:
    # YAML 1.1 parses the bare key `on:` as boolean True, so accept both forms.
    trigger = doc.get("on", doc.get(True))
    if not isinstance(trigger, dict):
        return []
    schedules = trigger.get("schedule") or []
    if not isinstance(schedules, list):
        return []
    name = doc.get("name") or Path(rel).stem
    out = []
    for entry in schedules:
        if isinstance(entry, dict) and (expr := entry.get("cron")):
            out.append(_Schedule(str(expr), rel, _line_of(raw, str(expr)), name, None))
    return out


def _from_k8s_cronjob(doc: dict, rel: str, raw: str) -> list[_Schedule]:
    if doc.get("kind") != "CronJob":
        return []
    spec = doc.get("spec") or {}
    expr = spec.get("schedule")
    if not expr:
        return []
    name = (doc.get("metadata") or {}).get("name") or "cronjob"
    return [_Schedule(str(expr), rel, _line_of(raw, str(expr)), name, spec.get("timeZone"))]


def _line_of(raw: str, needle: str) -> int:
    for n, line in enumerate(raw.splitlines(), start=1):
        if needle in line:
            return n
    return 0


def _firings_in_window(expr: str, window: Window, tz_name: str | None) -> list[datetime]:
    """Every firing of `expr` that lands inside [window.start, window.end)."""
    tz = timezone.utc
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone.utc

    start = window.start.astimezone(tz)
    end = window.end.astimezone(tz)

    try:
        # Seed one minute before the window so a firing exactly at start counts.
        it = croniter(expr, start - timedelta(minutes=1))
    except (CroniterBadCronError, ValueError):
        return []

    hits: list[datetime] = []
    for _ in range(_MAX_FIRINGS):
        nxt = it.get_next(datetime)
        if nxt >= end:
            break
        if nxt >= start:
            hits.append(nxt)
    return hits
