"""Ledger behaviour.

The ledger carries two guarantees the rest of the system leans on: an append-only
audit history, and idempotency for outward-facing actions. The second one is the
reason this file exists -- without it the nightly job reopens the same pull
request every night, which is the fastest way to get a tool like this banned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from policyforge.detect.base import Finding, Outcome, RuleResult
from policyforge.ledger import Ledger


@pytest.fixture
def led(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "test.sqlite")


def result(repo="payments-api", rule="registry-redirect", outcome=Outcome.VIOLATING,
           artifact="pol-1", findings=None, error=None) -> RuleResult:
    return RuleResult(
        artifact_id=artifact, rule_id=rule, repo=repo, outcome=outcome,
        findings=findings if findings is not None else
        [Finding(file="Dockerfile", line=1, excerpt="FROM ubuntu:latest")],
        error=error,
    )


class TestSchemaBootstrap:
    def test_creates_file_and_tables(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "l.sqlite"
        Ledger(path)
        assert path.exists(), "ledger must create its parent directories"

    def test_reopening_is_safe(self, tmp_path):
        path = tmp_path / "l.sqlite"
        run = Ledger(path).start_run()
        again = Ledger(path)
        assert again.latest_status() == []
        assert run


class TestRuns:
    def test_run_ids_are_unique(self, led):
        assert led.start_run() != led.start_run()

    def test_finish_run_sets_timestamp(self, led):
        run_id = led.start_run()
        led.finish_run(run_id)
        with led._conn() as c:
            row = c.execute("SELECT finished_at FROM runs WHERE run_id=?",
                            (run_id,)).fetchone()
        assert row["finished_at"] is not None


class TestChecks:
    def test_record_returns_row_count(self, led):
        run_id = led.start_run()
        assert led.record(run_id, [result(), result(repo="data-platform")]) == 2

    def test_findings_round_trip_as_json(self, led):
        run_id = led.start_run()
        led.record(run_id, [result()])
        rows = led.history("payments-api")
        assert rows[0]["finding_count"] == 1
        assert "FROM ubuntu:latest" in rows[0]["findings"]

    def test_error_results_are_recorded_not_dropped(self, led):
        """An unevaluable rule must leave a trace -- otherwise a broken detector
        looks identical to a compliant repo in the history."""
        run_id = led.start_run()
        led.record(run_id, [result(outcome=Outcome.ERROR, findings=[],
                                   error="ripgrep failed")])
        rows = led.history("payments-api")
        assert rows[0]["outcome"] == "error"
        assert rows[0]["error"] == "ripgrep failed"

    def test_history_filters_by_artifact(self, led):
        run_id = led.start_run()
        led.record(run_id, [result(artifact="pol-1"), result(artifact="pol-2")])
        assert len(led.history("payments-api")) == 2
        assert len(led.history("payments-api", artifact_id="pol-2")) == 1


class TestLatestStatus:
    def test_reports_only_the_most_recent_outcome(self, led):
        """A repo that was violating and is now clear must read as clear."""
        first = led.start_run()
        led.record(first, [result(outcome=Outcome.VIOLATING)])
        second = led.start_run()
        led.record(second, [result(outcome=Outcome.CLEAR, findings=[])])

        rows = led.latest_status()
        assert len(rows) == 1, "one row per (artifact, rule, repo)"
        assert rows[0]["outcome"] == "clear"

    def test_separates_distinct_triples(self, led):
        run_id = led.start_run()
        led.record(run_id, [
            result(repo="payments-api", rule="registry-redirect"),
            result(repo="payments-api", rule="digest-pinning"),
            result(repo="data-platform", rule="registry-redirect"),
        ])
        assert len(led.latest_status()) == 3


class TestActionIdempotency:
    def test_first_claim_succeeds(self, led):
        assert led.claim_action("pol-1", "rule-1", "payments-api", "pull_request") is True

    def test_second_claim_is_refused(self, led):
        """This is the guarantee that stops duplicate PRs."""
        led.claim_action("pol-1", "rule-1", "payments-api", "pull_request")
        assert led.claim_action("pol-1", "rule-1", "payments-api", "pull_request") is False

    def test_claims_are_scoped_per_repo_and_rule(self, led):
        led.claim_action("pol-1", "rule-1", "payments-api", "pull_request")
        assert led.claim_action("pol-1", "rule-1", "data-platform", "pull_request") is True
        assert led.claim_action("pol-1", "rule-2", "payments-api", "pull_request") is True
        assert led.claim_action("pol-2", "rule-1", "payments-api", "pull_request") is True

    def test_reclaim_refused_even_after_close(self, led):
        """A closed action must not be reopened by the next nightly run. If a human
        rejected the PR, reopening it is exactly the nagging behaviour to avoid."""
        led.claim_action("pol-1", "rule-1", "payments-api", "pull_request")
        led.close_action("pol-1", "rule-1", "payments-api", "rejected", "not applicable")
        assert led.claim_action("pol-1", "rule-1", "payments-api", "pull_request") is False

    def test_attach_url_and_read_back(self, led):
        led.claim_action("pol-1", "rule-1", "payments-api", "pull_request")
        led.attach_url("pol-1", "rule-1", "payments-api", "https://github.com/x/y/pull/7")
        row = led.open_actions()[0]
        assert row["url"].endswith("/pull/7")
        assert row["state"] == "open"

    def test_closed_actions_leave_open_list(self, led):
        led.claim_action("pol-1", "rule-1", "payments-api", "pull_request")
        led.claim_action("pol-1", "rule-1", "data-platform", "pull_request")
        led.close_action("pol-1", "rule-1", "payments-api", "merged")
        assert [r["repo"] for r in led.open_actions()] == ["data-platform"]

    def test_rejection_reason_is_retained(self, led):
        """Rejection reasons are the feedback signal fed back to the compiler."""
        led.claim_action("pol-1", "rule-1", "payments-api", "pull_request")
        led.close_action("pol-1", "rule-1", "payments-api", "rejected",
                         "base image is vendor-pinned by contract")
        with led._conn() as c:
            row = c.execute("SELECT reason FROM actions WHERE repo=?",
                            ("payments-api",)).fetchone()
        assert "vendor-pinned" in row["reason"]

# Compiler dedupe (what used to be TestSourceDedupe here) no longer lives on
# the ledger -- see Registry.source_refs() and test_compiler.py's
# TestPipelineFunnel / TestAmendmentResolution for its coverage now.
