"""Orchestration: messages in, proposed artifacts out.

The order of operations is the cost design. Ledger dedupe runs before triage,
triage runs before compilation, and compilation runs once per announcement --
never once per repository. A message already seen costs one SQLite lookup; a
message that is noise costs one cheap call; only a genuine obligation reaches the
expensive model.

Nothing here writes an `active` artifact. Everything lands as `proposed`, which
is what makes the registry pull request the single review gate between model
output and action on someone's repository.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from ..ledger import Ledger
from ..registry import Registry
from ..schema import Artifact, Kind, Source, Status
from ..sources.base import Message, SourceAdapter
from .engine import Engine
from .models import ProposedArtifact, ProposedRule, TriageVerdict

log = logging.getLogger(__name__)

_ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]{2,78}$")


@dataclass
class Outcome:
    """What happened to one message."""

    ref: str
    disposition: str          # noise | duplicate | compiled | amendment | rejected | error
    reason: str = ""
    artifact_id: str | None = None
    verdict: TriageVerdict | None = None


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)

    def count(self, disposition: str) -> int:
        return sum(1 for o in self.outcomes if o.disposition == disposition)

    @property
    def compiled(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.disposition == "compiled"]


def slugify(raw: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:79]


def to_artifact(proposal: ProposedArtifact, message: Message) -> Artifact:
    """Convert a model proposal into a real artifact.

    This is the validation boundary. The model chooses the rule content; it does
    not get to choose provenance or status. Source is reconstructed from the
    message we actually read, and status is forced to `proposed` -- a model that
    emitted `status: active` would otherwise be self-approving.
    """
    artifact_id = slugify(proposal.id)
    if not _ID_OK.match(artifact_id):
        raise ValueError(f"unusable artifact id from model: {proposal.id!r}")

    payload: dict = {
        "kind": proposal.kind,
        "id": artifact_id,
        "title": proposal.title,
        "status": Status.PROPOSED,
        "severity": proposal.severity,
        "confidence": proposal.confidence,
        "notes": proposal.reasoning,
        "source": Source(
            type=message.source_type,
            ref=message.ref,
            permalink=message.permalink,
            captured_at=message.posted_at,
            excerpt=message.truncated(2000),
        ),
        "rules": [_to_rule(r) for r in proposal.rules],
    }

    if proposal.kind == "event":
        if not (proposal.window_start and proposal.window_end):
            raise ValueError("event proposal is missing window_start/window_end")
        payload["window"] = {"start": proposal.window_start, "end": proposal.window_end}
    else:
        payload["deadline"] = proposal.deadline

    return Artifact(**payload)


def _to_rule(rule: ProposedRule) -> dict:
    """Restore the nesting the flat proposal shape collapsed."""
    d = rule.detect
    if d.type == "file_pattern":
        if not d.regex:
            raise ValueError(f"rule {rule.id}: file_pattern requires a regex")
        detect: dict = {
            "type": "file_pattern",
            "globs": d.globs,
            "regex": d.regex,
            "absent": d.absent,
        }
    else:
        detect = {"type": "schedule_intersects"}
        if d.globs:
            detect["globs"] = d.globs
        if d.depends_on_regex:
            detect["depends_on_regex"] = d.depends_on_regex

    out: dict = {
        "id": slugify(rule.id)[:63],
        "description": rule.description,
        "detect": detect,
        "verify": rule.verify,
    }

    if rule.autofix:
        a = rule.autofix
        fix: dict = {"type": a.type}
        if a.type == "prefix_rewrite":
            fix["to"] = a.to or ""
        elif a.type == "regex_replace":
            fix["pattern"] = a.pattern or ""
            fix["replacement"] = a.replacement or ""
        elif a.type == "shift_schedule":
            fix["by_hours"] = a.by_hours if a.by_hours is not None else 0
        else:
            fix["instructions"] = a.instructions or "Resolve manually."
        out["autofix"] = fix
    return out


@dataclass
class Pipeline:
    engine: Engine
    registry_root: Path
    ledger: Ledger
    min_confidence: float = 0.4

    def run(
        self,
        source: SourceAdapter,
        since: datetime,
        *,
        dry_run: bool = False,
        limit: int | None = None,
    ) -> Report:
        report = Report()
        registry = Registry.load(self.registry_root)
        known_ids = {a.id for a in registry.artifacts}

        processed = 0
        for message in source.fetch(since):
            if limit is not None and processed >= limit:
                break
            processed += 1
            report.outcomes.append(
                self._handle(message, registry, known_ids, report, dry_run)
            )
        return report

    def _handle(self, message, registry, known_ids, report, dry_run) -> Outcome:
        # 1. Cheapest possible check first: have we already seen this ref?
        if self.ledger.seen_source(message.ref):
            return Outcome(message.ref, "duplicate", "already processed")

        # 2. Cheap model.
        try:
            verdict = self.engine.triage(message)
        except Exception as exc:
            log.warning("triage failed for %s: %s", message.ref, exc)
            return Outcome(message.ref, "error", f"triage: {exc}")

        if not verdict.actionable or verdict.kind == "none":
            if not dry_run:
                self.ledger.mark_source(message.ref, "noise")
            return Outcome(message.ref, "noise", verdict.reason, verdict=verdict)

        if verdict.kind == "amendment":
            # Amendments need the artifact they target, which Phase 3 resolves
            # from the thread parent. Recorded rather than silently dropped so
            # the registry does not quietly go stale.
            return Outcome(
                message.ref, "amendment",
                f"amends an earlier obligation: {verdict.reason}", verdict=verdict,
            )

        # 3. Expensive model, once per announcement.
        try:
            proposal = self.engine.compile(message)
        except Exception as exc:
            log.warning("compile failed for %s: %s", message.ref, exc)
            return Outcome(message.ref, "error", f"compile: {exc}", verdict=verdict)

        if proposal.confidence < self.min_confidence:
            return Outcome(
                message.ref, "rejected",
                f"confidence {proposal.confidence:.2f} below threshold "
                f"{self.min_confidence:.2f}",
                verdict=verdict,
            )

        # 4. Our schema is the arbiter, not the model's JSON.
        try:
            artifact = to_artifact(proposal, message)
        except (ValueError, ValidationError) as exc:
            return Outcome(message.ref, "rejected", f"invalid artifact: {exc}",
                           verdict=verdict)

        artifact = self._deduplicate_id(artifact, known_ids)
        known_ids.add(artifact.id)

        if not dry_run:
            path = self._write(artifact)
            report.written.append(path)
            self.ledger.mark_source(message.ref, "compiled", artifact_id=artifact.id)

        return Outcome(message.ref, "compiled", proposal.reasoning,
                       artifact_id=artifact.id, verdict=verdict)

    @staticmethod
    def _deduplicate_id(artifact: Artifact, known_ids: set[str]) -> Artifact:
        if artifact.id not in known_ids:
            return artifact
        for n in range(2, 100):
            candidate = f"{artifact.id[:76]}-{n}"
            if candidate not in known_ids:
                return artifact.model_copy(update={"id": candidate})
        raise ValueError(f"cannot find a free id near {artifact.id}")

    def _write(self, artifact: Artifact) -> Path:
        subdir = "events" if artifact.kind is Kind.EVENT else "policies"
        out = self.registry_root / subdir
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{artifact.id}.json"
        path.write_text(
            json.dumps(
                artifact.model_dump(mode="json", exclude_none=True),
                indent=2, sort_keys=False,
            )
            + "\n"
        )
        return path
