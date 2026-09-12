"""Schema invariants. A malformed artifact must fail loudly at load time --
a silently-dropped policy is indistinguishable from one that passes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from policyforge.schema import Artifact, Kind, Severity, Status

UTC = timezone.utc


def make(source_stub, simple_rule, **over) -> Artifact:
    base = dict(
        kind="policy", id="test-policy", title="t",
        source=source_stub, rules=[simple_rule],
    )
    return Artifact(**{**base, **over})


def test_event_requires_window(source_stub, simple_rule):
    with pytest.raises(ValidationError, match="requires a window"):
        make(source_stub, simple_rule, kind="event")


def test_policy_rejects_window(source_stub, simple_rule):
    with pytest.raises(ValidationError, match="only valid on kind=event"):
        make(source_stub, simple_rule,
             window={"start": datetime(2026, 10, 3, 2, tzinfo=UTC),
                     "end": datetime(2026, 10, 3, 6, tzinfo=UTC)})


def test_event_rejects_deadline(source_stub, simple_rule):
    with pytest.raises(ValidationError, match="deadline is meaningless"):
        make(source_stub, simple_rule, kind="event", deadline=date(2027, 1, 1),
             window={"start": datetime(2026, 10, 3, 2, tzinfo=UTC),
                     "end": datetime(2026, 10, 3, 6, tzinfo=UTC)})


def test_window_must_be_ordered(source_stub, simple_rule):
    with pytest.raises(ValidationError, match="must be after"):
        make(source_stub, simple_rule, kind="event",
             window={"start": datetime(2026, 10, 3, 6, tzinfo=UTC),
                     "end": datetime(2026, 10, 3, 2, tzinfo=UTC)})


def test_superseded_requires_pointer(source_stub, simple_rule):
    with pytest.raises(ValidationError, match="requires superseded_by"):
        make(source_stub, simple_rule, status="superseded")


def test_duplicate_rule_ids_rejected(source_stub, simple_rule):
    with pytest.raises(ValidationError, match="rule ids must be unique"):
        make(source_stub, simple_rule, rules=[simple_rule, dict(simple_rule)])


def test_uncompilable_regex_rejected(source_stub, simple_rule):
    bad = {**simple_rule, "detect": {**simple_rule["detect"], "regex": "([unclosed"}}
    with pytest.raises(ValidationError, match="does not compile"):
        make(source_stub, bad, rules=[bad])


def test_extra_fields_rejected(source_stub, simple_rule):
    """Typos in an artifact must be errors, not silently-ignored keys."""
    with pytest.raises(ValidationError):
        make(source_stub, simple_rule, sevrity="blocking")


class TestBlockingGate:
    """A blocking policy with a future deadline warns now, fails CI later."""

    def test_not_blocking_before_deadline(self, source_stub, simple_rule):
        a = make(source_stub, simple_rule, severity="blocking",
                 deadline=date(2027, 1, 15), status="active")
        assert a.is_blocking(datetime(2026, 12, 1, tzinfo=UTC)) is False

    def test_blocking_on_deadline_day(self, source_stub, simple_rule):
        a = make(source_stub, simple_rule, severity="blocking",
                 deadline=date(2027, 1, 15), status="active")
        assert a.is_blocking(datetime(2027, 1, 15, tzinfo=UTC)) is True

    def test_blocking_without_deadline_is_immediate(self, source_stub, simple_rule):
        a = make(source_stub, simple_rule, severity="blocking", status="active")
        assert a.is_blocking() is True

    def test_non_blocking_severity_never_blocks(self, source_stub, simple_rule):
        a = make(source_stub, simple_rule, severity="high",
                 deadline=date(2020, 1, 1), status="active")
        assert a.is_blocking() is False


class TestEnforceability:
    def test_proposed_is_not_enforced(self, source_stub, simple_rule):
        assert make(source_stub, simple_rule, status="proposed").is_enforceable() is False

    def test_retired_is_not_enforced(self, source_stub, simple_rule):
        assert make(source_stub, simple_rule, status="retired").is_enforceable() is False

    def test_event_enforceable_before_and_during_window(self, source_stub, simple_rule):
        start = datetime(2026, 10, 3, 2, tzinfo=UTC)
        a = make(source_stub, simple_rule, kind="event", status="active",
                 window={"start": start, "end": start + timedelta(hours=4)})
        assert a.is_enforceable(start - timedelta(days=20)) is True   # pre-flight
        assert a.is_enforceable(start + timedelta(hours=1)) is True   # during
        assert a.is_enforceable(start + timedelta(hours=5)) is False  # expired

    def test_event_self_expires(self, source_stub, simple_rule):
        start = datetime(2026, 10, 3, 2, tzinfo=UTC)
        a = make(source_stub, simple_rule, kind="event", status="active",
                 window={"start": start, "end": start + timedelta(hours=4)})
        assert a.is_expired(start + timedelta(hours=1)) is False
        assert a.is_expired(start + timedelta(hours=4)) is True


class TestScoping:
    def test_empty_include_means_all(self, source_stub, simple_rule):
        assert make(source_stub, simple_rule).applies_to("anything") is True

    def test_exclude_wins_over_include(self, source_stub, simple_rule):
        a = make(source_stub, simple_rule,
                 include_repos=["a", "b"], exclude_repos=["b"])
        assert a.applies_to("a") is True
        assert a.applies_to("b") is False
        assert a.applies_to("c") is False
