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
from ..schema import Amendment, Artifact, Kind, Severity, Source, Status
from ..sources.base import Message, SourceAdapter
from .engine import Engine
from .models import ProposedAmendment, ProposedArtifact, ProposedRule, TriageVerdict

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
    # Artifacts changed in place by an amendment. Kept apart from `written`
    # because the two need different things said about them: a written artifact
    # is inert until promoted, an amended one may already be active.
    amended: list[Path] = field(default_factory=list)

    def count(self, disposition: str) -> int:
        return sum(1 for o in self.outcomes if o.disposition == disposition)

    @property
    def compiled(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.disposition == "compiled"]


def _serialise(artifact: Artifact) -> str:
    """The one way an artifact is written to disk.

    Single writer on purpose. Amendments rewrite files that already exist, and a
    reviewer reading the diff should see the deadline that moved -- not a
    reformatting of everything around it.
    """
    return json.dumps(
        artifact.model_dump(mode="json", exclude_none=True),
        indent=2, sort_keys=False,
    ) + "\n"


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


def amendment_changes(artifact: Artifact, proposal: ProposedAmendment) -> dict:
    """The fields an amendment actually moves, relative to the artifact today.

    Separate from applying them because "the model proposed no change" and "the
    model proposed a change that is already in effect" are both no-ops, and a
    no-op must not produce a registry commit. The amendment prompt asks for no
    changed fields when a follow-up merely restates the rule, so this is the
    common case, not an edge case.
    """
    changes: dict = {}

    if proposal.retire and artifact.status is not Status.RETIRED:
        changes["status"] = Status.RETIRED

    if proposal.new_severity and proposal.new_severity != artifact.severity:
        changes["severity"] = Severity(proposal.new_severity)

    if proposal.new_deadline and proposal.new_deadline != artifact.deadline:
        if artifact.kind is not Kind.POLICY:
            raise ValueError("a deadline cannot be set on an event; use a window")
        changes["deadline"] = proposal.new_deadline

    if proposal.new_window_start or proposal.new_window_end:
        if artifact.kind is not Kind.EVENT or artifact.window is None:
            raise ValueError("a window cannot be set on a policy; use a deadline")
        window = {
            "start": proposal.new_window_start or artifact.window.start,
            "end": proposal.new_window_end or artifact.window.end,
        }
        if window != {"start": artifact.window.start, "end": artifact.window.end}:
            changes["window"] = window

    return changes


def apply_amendment(
    artifact: Artifact,
    proposal: ProposedAmendment,
    message: Message,
    changes: dict,
    now: datetime | None = None,
) -> Artifact:
    """Apply an amendment to an existing artifact.

    The mirror of `to_artifact`, and the same boundary: the model proposes which
    fields move, our code decides which it is allowed to touch. An amendment may
    move a deadline or a window, change severity, or retire the obligation. It
    may not touch the artifact's identity, its rules, or its provenance, and it
    may not promote anything to `active` -- a follow-up message that could do any
    of those would be rewriting what a human already reviewed, without review.

    Status is otherwise carried through untouched. Amending an `active` artifact
    is deliberately allowed: the registry pull request is still the gate, and the
    diff is what a reviewer reads.
    """
    if not changes:
        raise ValueError("amendment proposes no change")

    applied_at = now or datetime.now(timezone.utc)
    record = Amendment(
        source=Source(
            type=message.source_type,
            ref=message.ref,
            permalink=message.permalink,
            captured_at=message.posted_at,
            excerpt=message.truncated(2000),
        ),
        summary=proposal.change_summary,
        applied_at=applied_at,
        confidence=proposal.confidence,
    )

    # Rebuilt through the constructor, not model_copy: model_copy skips
    # validation, and an amendment that produces an invalid artifact -- a
    # deadline landing on an event, an inverted window -- has to fail here
    # rather than be written to the registry.
    data = artifact.model_dump()
    data.update(changes)
    data["amendments"] = [*data["amendments"], record.model_dump()]
    data["updated_at"] = applied_at
    return Artifact(**data)


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
class _RunState:
    """Everything one run accumulates. Threaded explicitly so a run holds no
    state on the Pipeline itself and two runs cannot contaminate each other."""

    registry: Registry
    report: Report
    dry_run: bool
    known_ids: set[str] = field(default_factory=set)
    # source ref -> artifact id, for artifacts compiled during *this* run. The
    # ledger only learns them at write time, so without this a follow-up could
    # not find a parent compiled minutes earlier -- and under --dry-run, where
    # nothing is ever recorded, it could never find one at all.
    produced: dict[str, str] = field(default_factory=dict)


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
        registry = Registry.load(self.registry_root)
        state = _RunState(
            registry=registry,
            report=Report(),
            dry_run=dry_run,
            known_ids={a.id for a in registry.artifacts},
        )

        processed = 0
        for message in source.fetch(since):
            if limit is not None and processed >= limit:
                break
            processed += 1
            state.report.outcomes.append(self._handle(message, state))
        return state.report

    def _handle(self, message: Message, state: _RunState) -> Outcome:
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
            if not state.dry_run:
                self.ledger.mark_source(message.ref, "noise")
            return Outcome(message.ref, "noise", verdict.reason, verdict=verdict)

        if verdict.kind == "amendment":
            return self._amend(message, state, verdict)

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

        artifact = self._deduplicate_id(artifact, state.known_ids)
        state.known_ids.add(artifact.id)
        # Visible to the rest of this run whether or not it reaches disk, so a
        # follow-up later in the same batch can amend it.
        state.registry.artifacts.append(artifact)
        state.produced[message.ref] = artifact.id

        if not state.dry_run:
            path = self._write(artifact)
            state.registry.paths[artifact.id] = path
            state.report.written.append(path)
            self.ledger.mark_source(message.ref, "compiled", artifact_id=artifact.id)

        return Outcome(message.ref, "compiled", proposal.reasoning,
                       artifact_id=artifact.id, verdict=verdict)

    # ---- amendments -------------------------------------------------------

    def _amend(self, message: Message, state: _RunState, verdict) -> Outcome:
        """Apply a follow-up to the artifact it changes.

        Compiling a follow-up as a new artifact would leave two contradictory
        obligations active; dropping it as "just a reply" is how a registry goes
        quietly stale. Neither is acceptable, so it is resolved to a target and
        applied as a diff.
        """
        target, why = self._resolve_target(message, state)
        if target is None:
            # Deliberately not recorded as seen: this is the stale-registry
            # failure mode, and a rerun should retry it once the target exists.
            # Retrying costs one call to the cheap model, which is the stage the
            # funnel is built to make disposable.
            return Outcome(message.ref, "rejected",
                           f"amendment unresolved -- {why}", verdict=verdict)

        try:
            proposal = self.engine.amend(
                message,
                json.dumps(target.model_dump(mode="json", exclude_none=True), indent=2),
            )
        except Exception as exc:
            log.warning("amend failed for %s: %s", message.ref, exc)
            return Outcome(message.ref, "error", f"amend: {exc}", verdict=verdict)

        # The target came from the thread, not from the model. Same reasoning as
        # the triage ref echo: the model was shown exactly one artifact, so a
        # mismatched id is a transcription slip, not a different intention.
        if proposal.target_id != target.id:
            log.warning("amendment for %s named %r; applying to %r from the thread",
                        message.ref, proposal.target_id, target.id)

        if proposal.confidence < self.min_confidence:
            return Outcome(
                message.ref, "rejected",
                f"amendment confidence {proposal.confidence:.2f} below threshold "
                f"{self.min_confidence:.2f}",
                artifact_id=target.id, verdict=verdict,
            )

        try:
            changes = amendment_changes(target, proposal)
            amended = apply_amendment(target, proposal, message, changes)
        except ValueError as exc:
            # Covers both refusals and schema failures: pydantic's
            # ValidationError is a ValueError, so an amendment that would
            # produce an invalid artifact lands here too, unwritten.
            return Outcome(message.ref, "rejected", f"amendment not applied: {exc}",
                           artifact_id=target.id, verdict=verdict)

        state.registry.artifacts[state.registry.artifacts.index(target)] = amended

        if not state.dry_run:
            path = self._rewrite(amended, state.registry)
            state.report.amended.append(path)
            self.ledger.mark_source(message.ref, "amendment", artifact_id=amended.id)

        return Outcome(message.ref, "amendment",
                       f"{proposal.change_summary} [{', '.join(sorted(changes))}]",
                       artifact_id=amended.id, verdict=verdict)

    def _resolve_target(
        self, message: Message, state: _RunState
    ) -> tuple[Artifact | None, str]:
        """Find the artifact a follow-up amends, by walking its thread.

        The thread is the evidence, not the model: a reply to the announcement
        that produced artifact X amends X. That makes resolution a lookup, and
        keeps the choice of what to rewrite out of the model's hands entirely.
        """
        parent = message.thread_parent
        if not parent:
            return None, "not a threaded reply, so its target is unknowable"

        target_id = state.produced.get(parent) or self.ledger.source_artifact(parent)
        if not target_id:
            return None, f"the message it replies to produced no artifact ({parent})"

        target = state.registry.by_id(target_id)
        if target is None:
            return None, f"target '{target_id}' is no longer in the registry"
        if target.status in (Status.RETIRED, Status.SUPERSEDED):
            return None, f"target '{target_id}' is already {target.status}"
        return target, ""

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
        path.write_text(_serialise(artifact))
        return path

    def _rewrite(self, artifact: Artifact, registry: Registry) -> Path:
        """Overwrite an artifact in the file it was loaded from.

        Not `_write`: that derives the path from kind and id, and an artifact
        filed under any other name would be duplicated rather than amended --
        two files, one id, and a registry that no longer loads.
        """
        path = registry.paths.get(artifact.id)
        if path is None:
            return self._write(artifact)
        path.write_text(_serialise(artifact))
        return path
