"""Compilation: the funnel, and the conversion boundary.

Two things matter most here. The funnel must actually funnel -- if noise reaches
the expensive model the entire cost argument collapses. And the conversion from
model proposal to artifact must be the arbiter: a model that emits
`status: active` or bogus provenance must not get either.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from policyforge.compiler.engine import COMPILE_MODEL, TRIAGE_MODEL, Engine, Usage
from policyforge.compiler.models import ProposedArtifact, ProposedRule, TriageVerdict
from policyforge.compiler.pipeline import Pipeline, slugify, to_artifact
from policyforge.compiler.stub import StubClient
from policyforge.ledger import Ledger
from policyforge.schema import Kind, SourceType, Status
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
        ledger=Ledger(workspace / "l.sqlite"),
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
        assert len(compile_) == 3, "noise must not reach the expensive model"

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
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(slack_dir), EPOCH)

        client2 = StubClient()
        pipe2 = pipeline_for(workspace, client2)
        report = pipe2.run(FixtureSource(slack_dir), EPOCH)

        assert report.count("compiled") == 0
        assert report.count("duplicate") == 9
        assert [c for c in client2.calls if c[0] == COMPILE_MODEL] == []

    def test_dry_run_writes_nothing_and_does_not_mark_seen(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        report = pipe.run(FixtureSource(slack_dir), EPOCH, dry_run=True)
        assert report.written == []
        assert list((workspace / "registry" / "policies").glob("*.json")) == []
        assert not pipe.ledger.seen_source("C04PLATFORM/p1757600000000100")

    def test_limit_stops_early(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        report = pipe.run(FixtureSource(slack_dir), EPOCH, limit=2)
        assert len(report.outcomes) == 2


class TestPipelineRejection:
    def test_low_confidence_is_rejected_not_written(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient(), min_confidence=0.99)
        report = pipe.run(FixtureSource(slack_dir), EPOCH)
        assert report.count("compiled") == 0
        assert report.count("rejected") == 3
        assert report.written == []

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

    def test_id_collision_gets_a_suffix(self, workspace, slack_dir):
        pipe = pipeline_for(workspace, StubClient())
        pipe.run(FixtureSource(slack_dir), EPOCH)
        # Clear the dedupe ledger so the same messages compile again.
        pipe.ledger = Ledger(workspace / "fresh.sqlite")
        report = pipe.run(FixtureSource(slack_dir), EPOCH)
        ids = [o.artifact_id for o in report.compiled]
        assert any(i.endswith("-2") for i in ids), ids


class TestAmendmentRouting:
    def test_amendment_is_surfaced_not_silently_skipped(self, workspace, slack_dir):
        """A follow-up that moves a deadline must not be dropped as a reply --
        dropping it is how a registry silently goes stale."""
        report = pipeline_for(workspace, StubClient()).run(
            FixtureSource(slack_dir), EPOCH
        )
        amendments = [o for o in report.outcomes if o.disposition == "amendment"]
        assert len(amendments) == 1
        assert "Feb 1" in amendments[0].reason

    def test_plain_question_reply_is_noise_not_amendment(self, workspace, slack_dir):
        report = pipeline_for(workspace, StubClient()).run(
            FixtureSource(slack_dir), EPOCH
        )
        by_ref = {o.ref[-6:]: o for o in report.outcomes}
        assert by_ref["000101"].disposition == "noise"
        assert by_ref["000102"].disposition == "noise"
