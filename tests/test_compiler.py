"""Compilation: the funnel, and the conversion boundary.

Two things matter most here. The funnel must actually funnel -- if noise reaches
the expensive model the entire cost argument collapses. And the conversion from
model proposal to artifact must be the arbiter: a model that emits
`status: active` or bogus provenance must not get either.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from policyforge.compiler.engine import COMPILE_MODEL, TRIAGE_MODEL, Engine, Usage
from policyforge.compiler.models import (
    ProposedAmendment, ProposedArtifact, ProposedRule, TriageVerdict,
)
from policyforge.compiler.pipeline import (
    Pipeline, Report, _brief, _RunState, amendment_changes, apply_amendment,
    slugify, to_artifact,
)
from policyforge.compiler.stub import StubClient
from policyforge.registry import Registry
from policyforge.schema import Artifact, Kind, Severity, SourceType, Status
from policyforge.sources.base import Message
from policyforge.sources.fixture import FixtureSource

UTC = timezone.utc
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def slack_dir(repo_root: Path) -> Path:
    return repo_root / "fixtures" / "slack"


@pytest.fixture
def workspace(tmp_path: Path, repo_root: Path) -> Path:
    reg = tmp_path / "registry"
    (reg / "policies").mkdir(parents=True)
    (reg / "events").mkdir(parents=True)
    (reg / "catalog.yaml").write_text(
        (repo_root / "registry" / "catalog.yaml").read_text()
    )
    return tmp_path


def pipeline_for(workspace: Path, client=None, **kw) -> Pipeline:
    return Pipeline(
        engine=Engine(client=client or StubClient()),
        registry_root=workspace / "registry",
        **kw,
    )


def message(ref="C0/p000100", text="x", **kw) -> Message:
    return Message(ref=ref, text=text, posted_at=EPOCH,
                   source_type=SourceType.SLACK, **kw)


class TestFixtureSource:
    def test_reads_all_messages(self, slack_dir):
        assert len(list(FixtureSource(slack_dir).fetch(EPOCH))) == 10

    def test_since_filter_excludes_older(self, slack_dir):
        later = datetime(2026, 9, 12, 12, tzinfo=UTC)
        refs = [m.ref[-6:] for m in FixtureSource(slack_dir).fetch(later)]
        assert refs == ["000500", "000600", "000700", "000800"]

    def test_replies_carry_thread_parent(self, slack_dir):
        msgs = {m.ref[-6:]: m for m in FixtureSource(slack_dir).fetch(EPOCH)}
        assert msgs["000101"].is_reply
        assert msgs["000101"].thread_parent.endswith("000100")
        assert not msgs["000100"].is_reply

    def test_source_type_is_honoured(self, slack_dir):
        msgs = {m.ref[-6:]: m for m in FixtureSource(slack_dir).fetch(EPOCH)}
        assert msgs["000200"].source_type is SourceType.MAINTENANCE

    def test_empty_directory_is_not_an_error(self, tmp_path):
        assert list(FixtureSource(tmp_path).fetch(EPOCH)) == []


class TestMessageTruncation:
    def test_short_text_untouched(self):
        assert message(text="short").truncated(100) == "short"

    def test_long_text_is_bounded(self):
        out = message(text="x" * 5000).truncated(1000)
        assert len(out) < 1100
        assert "more chars" in out


class TestEngine:
    def test_triage_uses_the_cheap_model(self, slack_dir):
        client = StubClient()
        engine = Engine(client=client)
        engine.triage(next(iter(FixtureSource(slack_dir).fetch(EPOCH))))
        assert client.calls[0][0] == TRIAGE_MODEL

    def test_compile_uses_the_expensive_model(self, slack_dir):
        client = StubClient()
        engine = Engine(client=client)
        msg = next(m for m in FixtureSource(slack_dir).fetch(EPOCH)
                   if m.ref.endswith("000100"))
        engine.compile(msg)
        assert client.calls[0][0] == COMPILE_MODEL

    def test_ref_is_taken_from_our_message_not_the_model(self):
        """A transcription slip in the echoed ref must not misattribute a verdict."""
        engine = Engine(client=StubClient())
        verdict = engine.triage(message(ref="C0/p000100"))
        assert verdict.ref == "C0/p000100"

    def test_usage_is_accumulated(self):
        engine = Engine(client=StubClient())
        engine.triage(message(ref="C0/p000400"))
        engine.triage(message(ref="C0/p000600"))
        assert engine.usage.triage_calls == 2
        assert engine.usage.input_tokens > 0
        assert engine.usage.cache_read > 0

    def test_cost_estimate_is_finite(self):
        u = Usage(input_tokens=10_000, output_tokens=2_000, cache_read=5_000)
        assert u.estimated_cost_usd() > 0


class TestConversionBoundary:
    """The model proposes; our schema disposes."""

    def _proposal(self, **over) -> ProposedArtifact:
        base = dict(
            kind="policy", id="Some Policy 2027", title="t", severity="blocking",
            deadline="2027-01-15", confidence=0.9, reasoning="r",
            rules=[ProposedRule(
                id="rule-one", description="d",
                detect={"type": "file_pattern", "globs": ["**/Dockerfile"],
                        "regex": r"^FROM\s+x"},
            )],
        )
        return ProposedArtifact(**{**base, **over})

    def test_status_is_forced_to_proposed(self):
        """A model cannot self-approve its own output into enforcement."""
        art = to_artifact(self._proposal(), message())
        assert art.status is Status.PROPOSED

    def test_provenance_comes_from_the_message(self):
        msg = message(ref="C04/p999", permalink="https://slack/999")
        art = to_artifact(self._proposal(), msg)
        assert art.source.ref == "C04/p999"
        assert art.source.permalink == "https://slack/999"
        assert art.source.captured_at == msg.posted_at

    def test_id_is_slugified(self):
        assert to_artifact(self._proposal(), message()).id == "some-policy-2027"

    def test_unusable_id_is_rejected(self):
        with pytest.raises(ValueError, match="unusable artifact id"):
            to_artifact(self._proposal(id="!!"), message())

    def test_file_pattern_without_regex_is_rejected(self):
        bad = self._proposal(rules=[ProposedRule(
            id="r", description="d",
            detect={"type": "file_pattern", "globs": ["**/x"]},
        )])
        with pytest.raises(ValueError, match="requires a regex"):
            to_artifact(bad, message())

    def test_event_without_window_is_rejected(self):
        with pytest.raises(ValueError, match="missing window"):
            to_artifact(self._proposal(kind="event", deadline=None), message())

    def test_event_window_is_restored(self):
        art = to_artifact(
            self._proposal(kind="event", deadline=None,
                           window_start="2026-10-03T02:00:00Z",
                           window_end="2026-10-03T06:00:00Z"),
            message(),
        )
        assert art.kind is Kind.EVENT
        assert art.window.start.hour == 2

    def test_uncompilable_regex_is_caught_by_our_schema(self):
        """The model can emit anything; the artifact schema is the gate."""
        bad = self._proposal(rules=[ProposedRule(
            id="r", description="d",
            detect={"type": "file_pattern", "globs": ["**/x"], "regex": "([unclosed"},
        )])
        with pytest.raises(ValidationError):
            to_artifact(bad, message())

    def test_autofix_nesting_is_restored(self):
        art = to_artifact(self._proposal(rules=[ProposedRule(
            id="rw", description="d",
            detect={"type": "file_pattern", "globs": ["**/x"], "regex": "y"},
            autofix={"type": "prefix_rewrite", "to": "corp/"},
        )]), message())
        assert art.rules[0].autofix.type == "prefix_rewrite"
        assert art.rules[0].autofix.to == "corp/"

    def test_manual_autofix_gets_default_instructions(self):
        art = to_artifact(self._proposal(rules=[ProposedRule(
            id="mn", description="d",
            detect={"type": "file_pattern", "globs": ["**/x"], "regex": "y"},
            autofix={"type": "manual"},
        )]), message())
        assert art.rules[0].autofix.instructions


class TestSlugify:
    @pytest.mark.parametrize("raw,expected", [
        ("Artifactory Passthrough 2027-01", "artifactory-passthrough-2027-01"),
        ("  Spaces  Everywhere  ", "spaces-everywhere"),
        ("UPPER_snake.dots", "upper-snake-dots"),
        ("---leading-and-trailing---", "leading-and-trailing"),
    ])
    def test_slugify(self, raw, expected):
        assert slugify(raw) == expected


class TestPipelineFunnel:
    def test_only_actionable_messages_reach_the_expensive_model(self, workspace, slack_dir):
        """The entire cost argument rests on this ratio."""
        client = StubClient()
        pipe = pipeline_for(workspace, client)
        report = pipe.run(FixtureSource(slack_dir), EPOCH)

        assert report.count("compiled") == 3
        assert report.count("noise") == 6
        assert report.count("amendment") == 1

        triage = [c for c in client.calls if c[0] == TRIAGE_MODEL]
        compile_ = [c for c in client.calls if c[0] == COMPILE_MODEL]
        assert len(triage) == 10
        # Three announcements compiled, one follow-up amended. Six noise
        # messages stopped at the cheap stage, which is the whole argument.
        assert len(compile_) == 4, "noise must not reach the expensive model"

    def test_artifacts_are_written_as_proposed(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        report = pipe.run(FixtureSource(slack_dir), EPOCH)
        assert len(report.written) == 3
        for path in report.written:
            assert json.loads(path.read_text())["status"] == "proposed"

    def test_events_and_policies_are_filed_separately(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(slack_dir), EPOCH)
        assert len(list((workspace / "registry" / "events").glob("*.json"))) == 1
        assert len(list((workspace / "registry" / "policies").glob("*.json"))) == 2

    def test_second_run_compiles_nothing(self, workspace, slack_dir):
        """The durable guarantee is about the expensive stage, not every call:
        dedupe comes from the registry alone, which remembers a compiled or
        amended ref forever but has no way to remember noise -- so noise is
        triaged again (cheap), while nothing is ever recompiled (expensive)."""
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(slack_dir), EPOCH)

        client2 = StubClient()
        pipe2 = pipeline_for(workspace, client2)
        report = pipe2.run(FixtureSource(slack_dir), EPOCH)

        assert report.count("compiled") == 0
        assert report.count("amendment") == 0
        assert report.count("duplicate") == 4, "the 3 compiled + 1 amended refs"
        assert report.count("noise") == 6, "re-triaged -- nothing remembers noise"
        assert len(client2.calls) == 6, "one cheap call per re-triaged message"
        assert [c for c in client2.calls if c[0] == COMPILE_MODEL] == []

    def test_dry_run_writes_nothing_and_does_not_mark_seen(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        report = pipe.run(FixtureSource(slack_dir), EPOCH, dry_run=True)
        assert report.written == []
        assert list((workspace / "registry" / "policies").glob("*.json")) == []
        # Nothing durable happened: dedupe comes from the registry alone, and
        # a dry run touches no file in it.
        refs = Registry.load(workspace / "registry").source_refs()
        assert "C04PLATFORM/p1757600000000100" not in refs

    def test_limit_stops_early(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        report = pipe.run(FixtureSource(slack_dir), EPOCH, limit=2)
        assert len(report.outcomes) == 2


class TestPipelineRejection:
    def test_low_confidence_is_rejected_not_written(self, workspace, slack_dir):
        """The threshold gates amendments too -- three compilations and one
        follow-up, none of them confident enough to act on."""
        pipe = pipeline_for(workspace, StubClient(), min_confidence=0.99)
        report = pipe.run(FixtureSource(slack_dir), EPOCH)
        assert report.count("compiled") == 0
        assert report.count("rejected") == 4
        assert report.written == []
        assert report.amended == []

    def test_triage_failure_is_recorded_not_fatal(self, workspace, slack_dir):
        client = StubClient(fail_on={"000100"})
        report = pipeline_for(workspace, client).run(FixtureSource(slack_dir), EPOCH)
        errors = [o for o in report.outcomes if o.disposition == "error"]
        assert len(errors) == 1
        assert "triage" in errors[0].reason
        assert report.count("compiled") == 2, "one failure must not abort the batch"

    def test_unknown_message_is_treated_as_noise(self, workspace, tmp_path):
        """The stub must never invent a policy for something it does not know."""
        src = tmp_path / "msgs"
        src.mkdir()
        (src / "m.json").write_text(json.dumps([{
            "ref": "C0/p999999", "text": "anything",
            "posted_at": "2026-09-12T00:00:00+00:00",
        }]))
        report = pipeline_for(workspace, StubClient()).run(FixtureSource(src), EPOCH)
        assert report.count("noise") == 1

    def test_id_collision_gets_a_suffix(self):
        """Two distinct messages whose compilations land on the same slug --
        orthogonal to dedupe, which is about one ref, not one id. Tested
        directly against the mechanism rather than through a full run: with
        dedupe now durable via the registry (see TestPipelineFunnel), there is
        no longer a way to force the *same* ref through the pipeline twice."""
        art = to_artifact(TestConversionBoundary()._proposal(), message())
        collided = Pipeline._deduplicate_id(art, known_ids={art.id})
        assert collided.id == f"{art.id}-2"
        assert Pipeline._deduplicate_id(art, known_ids=set()) is art, (
            "no collision must return the same object, not a copy"
        )


class TestAmendmentRouting:
    def test_amendment_is_surfaced_not_silently_skipped(self, workspace, slack_dir):
        """A follow-up that moves a deadline must not be dropped as a reply --
        dropping it is how a registry silently goes stale."""
        report = pipeline_for(workspace, StubClient()).run(
            FixtureSource(slack_dir), EPOCH
        )
        amendments = [o for o in report.outcomes if o.disposition == "amendment"]
        assert len(amendments) == 1
        assert "2027-02-01" in amendments[0].reason
        assert amendments[0].artifact_id == "artifactory-docker-passthrough-2027-01"

    def test_plain_question_reply_is_noise_not_amendment(self, workspace, slack_dir):
        report = pipeline_for(workspace, StubClient()).run(
            FixtureSource(slack_dir), EPOCH
        )
        by_ref = {o.ref[-6:]: o for o in report.outcomes}
        assert by_ref["000101"].disposition == "noise"
        assert by_ref["000102"].disposition == "noise"


# ---------------------------------------------------------------------------
# Amendments
# ---------------------------------------------------------------------------

POLICY_ID = "artifactory-docker-passthrough-2027-01"
PARENT_REF = "C04PLATFORM/p1757600000000100"
AMEND_REF = "C04PLATFORM/p1757990000000700"


def artifact(**over) -> Artifact:
    base = dict(
        kind="policy", id="a-policy-2027", title="t", status=Status.ACTIVE,
        severity=Severity.HIGH, deadline=date(2027, 1, 15),
        source={"type": "slack", "ref": "C0/p1", "captured_at": EPOCH},
        rules=[{"id": "rule-a", "description": "d",
                "detect": {"type": "file_pattern", "globs": ["**/Dockerfile"],
                           "regex": "^FROM x"}}],
    )
    return Artifact(**{**base, **over})


def amendment(**over) -> ProposedAmendment:
    base = dict(target_id="a-policy-2027", change_summary="s", confidence=0.9)
    return ProposedAmendment(**{**base, **over})


def _only(tmp_path, name: str, messages: list, ref: str) -> Path:
    """A one-message source directory, for testing runs a day apart."""
    d = tmp_path / name
    d.mkdir()
    (d / "m.json").write_text(json.dumps([m for m in messages if m["ref"] == ref]))
    return d


class TestAmendmentChanges:
    """What an amendment is allowed to move, and what counts as movement."""

    def test_moved_deadline_is_a_change(self):
        art = artifact()
        changes = amendment_changes(art, amendment(new_deadline=date(2027, 2, 1)))
        assert changes == {"deadline": date(2027, 2, 1)}

    def test_restating_the_existing_deadline_is_not_a_change(self):
        """The prompt asks for no changed fields when a follow-up merely
        restates the rule. A restatement must not produce a registry commit."""
        assert amendment_changes(artifact(), amendment(new_deadline=date(2027, 1, 15))) == {}

    def test_empty_amendment_is_not_a_change(self):
        assert amendment_changes(artifact(), amendment()) == {}

    def test_severity_change_is_detected(self):
        changes = amendment_changes(artifact(), amendment(new_severity="blocking"))
        assert changes == {"severity": Severity.BLOCKING}

    def test_retire_sets_status(self):
        assert amendment_changes(artifact(), amendment(retire=True)) == {
            "status": Status.RETIRED
        }

    def test_retiring_an_already_retired_artifact_is_not_a_change(self):
        art = artifact(status=Status.RETIRED)
        assert amendment_changes(art, amendment(retire=True)) == {}

    def test_window_may_be_moved_at_one_end_only(self):
        art = artifact(
            kind="event", deadline=None,
            window={"start": datetime(2026, 10, 3, 2, tzinfo=UTC),
                    "end": datetime(2026, 10, 3, 6, tzinfo=UTC)},
        )
        changes = amendment_changes(
            art, amendment(new_window_end=datetime(2026, 10, 3, 8, tzinfo=UTC))
        )
        assert changes["window"]["start"] == datetime(2026, 10, 3, 2, tzinfo=UTC)
        assert changes["window"]["end"] == datetime(2026, 10, 3, 8, tzinfo=UTC)

    def test_deadline_on_an_event_is_refused(self):
        art = artifact(kind="event", deadline=None,
                       window={"start": EPOCH, "end": datetime(2026, 1, 2, tzinfo=UTC)})
        with pytest.raises(ValueError, match="deadline cannot be set on an event"):
            amendment_changes(art, amendment(new_deadline=date(2027, 2, 1)))

    def test_window_on_a_policy_is_refused(self):
        with pytest.raises(ValueError, match="window cannot be set on a policy"):
            amendment_changes(artifact(), amendment(new_window_start=EPOCH))


class TestApplyAmendment:
    """The second half of the conversion boundary."""

    def _apply(self, art, proposal, **kw):
        changes = amendment_changes(art, proposal)
        return apply_amendment(art, proposal, message(ref=AMEND_REF), changes, **kw)

    def test_a_no_op_is_refused(self):
        with pytest.raises(ValueError, match="no change"):
            self._apply(artifact(), amendment())

    def test_the_change_is_applied(self):
        out = self._apply(artifact(), amendment(new_deadline=date(2027, 2, 1)))
        assert out.deadline == date(2027, 2, 1)

    def test_provenance_of_the_change_travels_with_the_artifact(self):
        """An artifact whose deadline no longer matches its own source excerpt
        is misleading unless the thing that moved it is recorded on it."""
        out = self._apply(artifact(),
                          amendment(new_deadline=date(2027, 2, 1),
                                    change_summary="Cutoff moved to Feb 1."))
        assert len(out.amendments) == 1
        assert out.amendments[0].source.ref == AMEND_REF
        assert out.amendments[0].summary == "Cutoff moved to Feb 1."
        assert out.amendments[0].confidence == 0.9
        assert out.updated_at is not None
        # The original provenance is untouched.
        assert out.source.ref == "C0/p1"

    def test_amendments_accumulate_rather_than_replace(self):
        first = self._apply(artifact(), amendment(new_deadline=date(2027, 2, 1)))
        second = self._apply(first, amendment(new_deadline=date(2027, 3, 1)))
        assert [a.summary for a in second.amendments] == ["s", "s"]
        assert second.deadline == date(2027, 3, 1)

    def test_identity_and_rules_are_untouchable(self):
        art = artifact()
        out = self._apply(art, amendment(new_severity="info"))
        assert out.id == art.id
        assert out.kind == art.kind
        assert [r.id for r in out.rules] == [r.id for r in art.rules]

    def test_status_is_carried_through_not_reset(self):
        """Amending an active artifact leaves it active. The registry pull
        request is still the gate; the diff is what gets reviewed."""
        out = self._apply(artifact(status=Status.ACTIVE),
                          amendment(new_deadline=date(2027, 2, 1)))
        assert out.status is Status.ACTIVE

    def test_a_proposed_artifact_stays_proposed(self):
        out = self._apply(artifact(status=Status.PROPOSED),
                          amendment(new_deadline=date(2027, 2, 1)))
        assert out.status is Status.PROPOSED

    def test_retire_switches_enforcement_off(self):
        out = self._apply(artifact(), amendment(retire=True))
        assert out.status is Status.RETIRED
        assert out.is_enforceable(datetime(2027, 6, 1, tzinfo=UTC)) is False

    def test_an_invalid_result_raises_rather_than_being_written(self):
        """model_copy would skip validation here; the constructor must not."""
        art = artifact(kind="event", deadline=None,
                       window={"start": datetime(2026, 10, 3, 2, tzinfo=UTC),
                               "end": datetime(2026, 10, 3, 6, tzinfo=UTC)})
        prop = amendment(new_window_start=datetime(2026, 10, 3, 9, tzinfo=UTC))
        with pytest.raises(ValidationError, match="must be after"):
            self._apply(art, prop)


class TestAmendmentResolution:
    """Which artifact a follow-up amends is decided by the thread, not the model."""

    def test_follow_up_amends_the_artifact_its_parent_produced(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(slack_dir), EPOCH)

        art = Registry.load(workspace / "registry").by_id(POLICY_ID)
        assert art.deadline == date(2027, 2, 1), "the moved cutoff must be in effect"
        assert [a.source.ref for a in art.amendments] == [AMEND_REF]

    def test_no_second_artifact_is_created(self, workspace, slack_dir):
        """Compiling a follow-up afresh would leave two contradictory policies."""
        pipe = pipeline_for(workspace, StubClient())
        report = pipe.run(FixtureSource(slack_dir), EPOCH)
        assert len(list((workspace / "registry").rglob("*.json"))) == 3
        assert report.amended == [
            workspace / "registry" / "policies" / f"{POLICY_ID}.json"
        ]

    def test_the_amended_registry_still_loads(self, workspace, slack_dir):
        pipeline_for(workspace, StubClient()).run(FixtureSource(slack_dir), EPOCH)
        reg = Registry.load(workspace / "registry")
        assert len(reg.artifacts) == 3

    def test_amendment_resolves_against_a_parent_from_an_earlier_run(
        self, workspace, slack_dir, tmp_path
    ):
        """The realistic case: the announcement compiled last week, the
        follow-up arrives tonight. Resolution is a registry lookup -- durable
        with nothing but the git-tracked artifact file, no database at all."""
        full = json.loads((slack_dir / "platform-announcements.json").read_text())
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(_only(tmp_path, "day1", full, PARENT_REF)), EPOCH)
        assert Registry.load(workspace / "registry").by_id(POLICY_ID).deadline == date(2027, 1, 15)

        report = pipeline_for(workspace, StubClient()).run(
            FixtureSource(_only(tmp_path, "day2", full, AMEND_REF)), EPOCH
        )
        assert report.count("amendment") == 1
        assert Registry.load(workspace / "registry").by_id(POLICY_ID).deadline == date(2027, 2, 1)

    def test_an_amendment_is_not_reprocessed(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(slack_dir), EPOCH)
        assert AMEND_REF in Registry.load(workspace / "registry").source_refs()

        client2 = StubClient()
        report = pipeline_for(workspace, client2).run(FixtureSource(slack_dir), EPOCH)
        assert report.count("amendment") == 0
        assert [c for c in client2.calls if c[0] == COMPILE_MODEL] == []

    def test_an_unrooted_amendment_is_rejected_and_retried_later(
        self, workspace, tmp_path
    ):
        """An amendment with no resolvable target is the stale-registry failure
        mode. It must not be quietly marked done."""
        src = tmp_path / "msgs"
        src.mkdir()
        (src / "m.json").write_text(json.dumps([{
            "ref": "C0/p000700", "text": "moving the cutoff",
            "posted_at": "2026-09-12T00:00:00+00:00",
        }]))
        pipe = pipeline_for(workspace, StubClient())
        report = pipe.run(FixtureSource(src), EPOCH)

        assert report.count("amendment") == 0
        assert "unresolved" in report.outcomes[0].reason
        assert "not a threaded reply" in report.outcomes[0].reason

        # Must be retried, not quietly marked done: nothing durable recorded
        # this ref, so a second run sees it fresh rather than as a duplicate.
        again = pipe.run(FixtureSource(src), EPOCH)
        assert again.outcomes[0].disposition == "rejected"
        assert again.outcomes[0].reason == report.outcomes[0].reason

    def test_a_reply_to_noise_has_nothing_to_amend(self, workspace, tmp_path):
        src = tmp_path / "msgs"
        src.mkdir()
        (src / "m.json").write_text(json.dumps([
            {"ref": "C0/p000400", "text": "coffee machine",
             "posted_at": "2026-09-12T00:00:00+00:00"},
            {"ref": "C0/p000700", "text": "moving the cutoff",
             "posted_at": "2026-09-12T01:00:00+00:00",
             "thread_parent": "C0/p000400"},
        ]))
        report = pipeline_for(workspace, StubClient()).run(FixtureSource(src), EPOCH)
        assert report.outcomes[1].disposition == "rejected"
        assert "produced no artifact" in report.outcomes[1].reason

    def test_a_retired_target_is_not_reanimated(self, workspace, slack_dir, tmp_path):
        full = json.loads((slack_dir / "platform-announcements.json").read_text())
        pipeline_for(workspace, StubClient()).run(
            FixtureSource(_only(tmp_path, "day1", full, PARENT_REF)), EPOCH
        )

        path = workspace / "registry" / "policies" / f"{POLICY_ID}.json"
        raw = json.loads(path.read_text())
        raw["status"] = "retired"
        path.write_text(json.dumps(raw))

        # No ledger priming needed: the artifact is still on disk with its
        # original source.ref, so a fresh Pipeline resolves the parent from
        # the registry alone.
        report = pipeline_for(workspace, StubClient()).run(
            FixtureSource(_only(tmp_path, "day2", full, AMEND_REF)), EPOCH
        )
        amend = next(o for o in report.outcomes if o.ref == AMEND_REF)
        assert amend.disposition == "rejected"
        assert "already retired" in amend.reason

    def test_dry_run_amends_nothing_on_disk(self, workspace, slack_dir, tmp_path):
        """Once an amendment is durably applied, its ref is itself recorded
        (Amendment.source.ref), so reprocessing it is correctly a duplicate --
        this has to dry-run one that has not been applied yet."""
        full = json.loads((slack_dir / "platform-announcements.json").read_text())
        pipeline_for(workspace, StubClient()).run(
            FixtureSource(_only(tmp_path, "day1", full, PARENT_REF)), EPOCH
        )
        before = (workspace / "registry" / "policies" / f"{POLICY_ID}.json").read_text()

        report = pipeline_for(workspace, StubClient()).run(
            FixtureSource(_only(tmp_path, "day2", full, AMEND_REF)), EPOCH, dry_run=True
        )
        assert report.count("amendment") == 1, "still reported"
        assert report.amended == []
        assert (workspace / "registry" / "policies"
                / f"{POLICY_ID}.json").read_text() == before

    def test_amendment_rewrites_the_file_it_was_loaded_from(
        self, workspace, slack_dir, tmp_path
    ):
        """Filenames are not guaranteed to match ids. Writing to the derived
        name instead would leave two files with one id, and a registry that
        no longer loads."""
        full = json.loads((slack_dir / "platform-announcements.json").read_text())
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(_only(tmp_path, "day1", full, PARENT_REF)), EPOCH)

        canonical = workspace / "registry" / "policies" / f"{POLICY_ID}.json"
        odd = canonical.parent / "renamed-by-hand.json"
        canonical.rename(odd)

        report = pipe.run(FixtureSource(_only(tmp_path, "day2", full, AMEND_REF)), EPOCH)

        assert report.amended == [odd]
        assert not canonical.exists(), "the artifact must not be duplicated"
        assert json.loads(odd.read_text())["deadline"] == "2027-02-01"
        assert len(Registry.load(workspace / "registry").artifacts) == 1

    def test_the_diff_a_reviewer_reads_is_small(self, workspace, slack_dir):
        """The review is the diff, so the diff has to stay readable."""
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(slack_dir), EPOCH)
        path = workspace / "registry" / "policies" / f"{POLICY_ID}.json"

        # Re-create the pre-amendment state and compare.
        before = json.loads(path.read_text())
        after = json.loads(path.read_text())
        before["deadline"] = "2027-01-15"
        before["amendments"] = []
        before.pop("updated_at", None)

        changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
        assert sorted(changed) == ["amendments", "deadline", "updated_at"]


class _AmendClient(StubClient):
    """StubClient with the amendment stage overridden, to reach paths the canned
    happy-path response cannot."""

    def __init__(self, *, raises=False, **fields):
        super().__init__()
        self.raises = raises
        self.fields = fields

    def parse(self, *, model, system, user, schema):
        if schema is ProposedAmendment:
            self.calls.append((model, self._ref_from(user)))
            if self.raises:
                raise RuntimeError("model unavailable")
            return amendment(**self.fields), _usage()
        return super().parse(model=model, system=system, user=user, schema=schema)


def _usage():
    from policyforge.compiler.stub import _Usage
    return _Usage(900, 200, 800, 0)


class TestAmendmentFailureModes:
    """Every one of these leaves the target artifact untouched."""

    def _deadline_of(self, workspace):
        return Registry.load(workspace / "registry").by_id(POLICY_ID).deadline

    def test_a_failed_amend_call_is_recorded_not_fatal(self, workspace, slack_dir):
        report = pipeline_for(workspace, _AmendClient(raises=True)).run(
            FixtureSource(slack_dir), EPOCH
        )
        amend = next(o for o in report.outcomes if o.ref == AMEND_REF)
        assert amend.disposition == "error"
        assert "amend" in amend.reason
        assert report.count("compiled") == 3, "one failure must not abort the batch"
        assert self._deadline_of(workspace) == date(2027, 1, 15)

    def test_a_follow_up_that_changes_nothing_is_rejected(self, workspace, slack_dir):
        """The prompt asks for no changed fields when a reply merely restates the
        rule. That must not produce a registry commit."""
        client = _AmendClient(change_summary="Restates the existing cutoff.")
        report = pipeline_for(workspace, client).run(FixtureSource(slack_dir), EPOCH)
        amend = next(o for o in report.outcomes if o.ref == AMEND_REF)
        assert amend.disposition == "rejected"
        assert "no change" in amend.reason
        assert report.amended == []

    def test_an_amendment_that_would_invalidate_the_artifact_is_rejected(
        self, workspace, slack_dir
    ):
        """A window on a policy. Caught here rather than written to the registry."""
        client = _AmendClient(new_window_start=datetime(2027, 1, 1, tzinfo=UTC))
        report = pipeline_for(workspace, client).run(FixtureSource(slack_dir), EPOCH)
        amend = next(o for o in report.outcomes if o.ref == AMEND_REF)
        assert amend.disposition == "rejected"
        assert "window cannot be set on a policy" in amend.reason
        assert report.amended == []

    def test_a_target_known_but_missing_from_the_registry_is_rejected(self, workspace):
        """Defensive, and no longer reachable through a full run: known_refs
        and registry.artifacts are built from the same loaded list, so they
        cannot disagree in practice any more. Tested directly against
        _resolve_target so the check itself stays proven if that ever
        changes, rather than deleting it as dead code."""
        reg = Registry.load(workspace / "registry")
        state = _RunState(
            registry=reg, report=Report(), dry_run=False,
            known_refs={PARENT_REF: "an-id-not-in-the-registry"},
        )
        pipe = pipeline_for(workspace, StubClient())
        target, why = pipe._resolve_target(
            message(ref=AMEND_REF, thread_parent=PARENT_REF), state
        )
        assert target is None
        assert "no longer in the registry" in why

    def test_rewriting_an_artifact_with_no_known_path_falls_back_to_writing_one(
        self, workspace
    ):
        """Only reachable for an artifact compiled and amended in one dry run,
        but a silent no-write would lose the amendment."""
        pipe = pipeline_for(workspace, StubClient())
        art = artifact(id=POLICY_ID)
        path = pipe._rewrite(art, Registry())
        assert path == workspace / "registry" / "policies" / f"{POLICY_ID}.json"
        assert json.loads(path.read_text())["id"] == POLICY_ID


EVENT_ID = "artifactory-upgrade-2026-10-03"
EVENT_REF = "C04PLATFORM/p1757900000000200"


class TestAmendingAnEvent:
    """Maintenance windows move at least as often as deadlines do."""

    def _amend_the_event(self, workspace, slack_dir, tmp_path, **fields):
        pipeline_for(workspace, StubClient()).run(FixtureSource(slack_dir), EPOCH)
        src = tmp_path / "followup"
        src.mkdir()
        # Trailing digits pick the canned triage verdict; the thread parent is
        # what actually decides the target.
        (src / "m.json").write_text(json.dumps([{
            "ref": "C0TEST/p000700", "text": "the window has moved",
            "posted_at": "2026-09-13T00:00:00+00:00", "thread_parent": EVENT_REF,
        }]))
        report = pipeline_for(workspace, _AmendClient(**fields)).run(
            FixtureSource(src), EPOCH
        )
        return report, Registry.load(workspace / "registry").by_id(EVENT_ID)

    def test_a_window_can_be_extended(self, workspace, slack_dir, tmp_path):
        report, event = self._amend_the_event(
            workspace, slack_dir, tmp_path,
            new_window_end=datetime(2026, 10, 3, 8, tzinfo=UTC),
            change_summary="Upgrade overran; window extended to 08:00 UTC.",
        )
        assert report.count("amendment") == 1
        assert event.window.start == datetime(2026, 10, 3, 2, tzinfo=UTC), "untouched"
        assert event.window.end == datetime(2026, 10, 3, 8, tzinfo=UTC)
        assert event.amendments[0].source.ref == "C0TEST/p000700"

    def test_the_thread_decides_the_target_not_the_model(
        self, workspace, slack_dir, tmp_path
    ):
        """_AmendClient names 'a-policy-2027'. The reply is threaded under the
        maintenance notice, so that is what gets amended."""
        _, event = self._amend_the_event(
            workspace, slack_dir, tmp_path,
            target_id="a-policy-2027",
            new_window_end=datetime(2026, 10, 3, 8, tzinfo=UTC),
        )
        assert event.window.end == datetime(2026, 10, 3, 8, tzinfo=UTC)
        assert Registry.load(workspace / "registry").by_id("a-policy-2027") is None

    def test_an_inverted_window_is_refused_by_our_schema(
        self, workspace, slack_dir, tmp_path
    ):
        """A start after the end validates cleanly as a *change* and only fails
        when the artifact is rebuilt. That rebuild is why it fails at all."""
        report, event = self._amend_the_event(
            workspace, slack_dir, tmp_path,
            new_window_start=datetime(2026, 10, 3, 9, tzinfo=UTC),
        )
        amend = report.outcomes[0]
        assert amend.disposition == "rejected"
        assert "amendment not applied" in amend.reason
        assert "must be after" in amend.reason
        assert event.window.end == datetime(2026, 10, 3, 6, tzinfo=UTC), "unchanged"
        assert event.amendments == []


class TestBrief:
    """The second, tighter cap -- the one the API-facing schema deliberately
    does not enforce, so an overlong real response fails here, after it is
    free to parse, rather than failing structured-output validation after it
    has already been paid for."""

    def test_short_text_is_untouched(self):
        assert _brief("fits fine", 1000) == "fits fine"

    def test_long_text_is_truncated_with_a_marker(self):
        out = _brief("x" * 2000, 1000)
        assert len(out) == 1000
        assert out.endswith("…")

    def test_boundary_length_is_not_truncated(self):
        assert _brief("x" * 1000, 1000) == "x" * 1000


class TestOverlongModelOutput:
    """The real failure this guards: a verbose response under high-effort
    thinking must not fail schema validation and waste a paid-for call."""

    def test_reasoning_schema_accepts_what_used_to_be_rejected(self):
        """The old cap (max_length=1000) is exactly what broke against the
        live model; the schema must tolerate it now."""
        ProposedArtifact(
            kind="policy", id="p", title="t", severity="warn",
            confidence=0.9, reasoning="x" * 1500,
            rules=[ProposedRule(
                id="r", description="d",
                detect={"type": "file_pattern", "globs": ["**/x"], "regex": "y"},
            )],
        )

    def test_overlong_reasoning_lands_truncated_in_notes(self):
        art = to_artifact(
            TestConversionBoundary()._proposal(reasoning="x" * 1500), message()
        )
        assert len(art.notes) == 1000
        assert art.notes.endswith("…")

    def test_overlong_change_summary_lands_truncated_in_the_amendment(self):
        out = apply_amendment(
            artifact(), amendment(new_deadline=date(2027, 2, 1),
                                  change_summary="x" * 800),
            message(ref=AMEND_REF), {"deadline": date(2027, 2, 1)},
        )
        assert len(out.amendments[0].summary) == 500
        assert out.amendments[0].summary.endswith("…")
