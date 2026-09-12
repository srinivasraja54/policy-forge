"""Detector behaviour, including the cases that matter most in review:
the dependency gate, and the refusal to report a tool failure as compliance."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from policyforge.detect import RepoTarget, evaluate_artifact
from policyforge.detect.base import Outcome
from policyforge.registry import Registry
from policyforge.schema import Artifact

UTC = timezone.utc


@pytest.fixture
def reg(registry_dir):
    return Registry.load(registry_dir)


def results_for(reg, artifact_id, repo, fixtures_dir):
    art = reg.by_id(artifact_id)
    assert art is not None, f"missing artifact {artifact_id}"
    return evaluate_artifact(art, RepoTarget(name=repo, path=fixtures_dir / repo))


def outcome_of(results, rule_id):
    return next(r for r in results if r.rule_id == rule_id)


POLICY = "artifactory-docker-passthrough-2027-01"
EVENT = "artifactory-upgrade-2026-10-03"


class TestFilePattern:
    def test_flags_public_registry(self, reg, fixtures_dir):
        res = outcome_of(results_for(reg, POLICY, "payments-api", fixtures_dir),
                         "registry-redirect")
        assert res.outcome is Outcome.VIOLATING
        files = {f.file for f in res.findings}
        assert "Dockerfile" in files
        assert "services/ml-trainer/Dockerfile" in files

    def test_compliant_repo_is_clear(self, reg, fixtures_dir):
        for rule in ("registry-redirect", "digest-pinning"):
            res = outcome_of(results_for(reg, POLICY, "web-ui", fixtures_dir), rule)
            assert res.outcome is Outcome.CLEAR, f"{rule} should be clear on web-ui"

    def test_negative_lookahead_excludes_internal_registry(self, reg, fixtures_dir):
        """The rule must not fire on artifactory.corp itself -- the PCRE2
        lookahead is what makes the rule expressible without a second pass."""
        res = outcome_of(results_for(reg, POLICY, "web-ui", fixtures_dir),
                         "registry-redirect")
        assert res.findings == []

    def test_digest_pinning_ignores_unpinnable_arg(self, reg, fixtures_dir):
        """FROM $BASE_IMAGE has no tag, so the digest rule must not claim it."""
        res = outcome_of(results_for(reg, POLICY, "payments-api", fixtures_dir),
                         "digest-pinning")
        assert {f.file for f in res.findings} == {"Dockerfile"}

    def test_missing_path_is_error_not_clear(self, reg, tmp_path):
        art = reg.by_id(POLICY)
        res = evaluate_artifact(art, RepoTarget(name="ghost", path=tmp_path / "nope"))
        assert all(r.outcome is Outcome.ERROR for r in res)
        assert all("does not exist" in (r.error or "") for r in res)


class TestScheduleIntersection:
    def test_daily_cron_inside_window_is_flagged(self, reg, fixtures_dir):
        res = outcome_of(results_for(reg, EVENT, "payments-api", fixtures_dir),
                         "jobs-in-window")
        assert res.outcome is Outcome.VIOLATING
        assert res.findings[0].excerpt == "0 3 * * *"
        assert "2026-10-03 03:00" in res.findings[0].detail

    def test_finds_both_workflow_and_k8s_cronjob(self, reg, fixtures_dir):
        res = outcome_of(results_for(reg, EVENT, "data-platform", fixtures_dir),
                         "jobs-in-window")
        assert res.count == 2
        assert {f.excerpt for f in res.findings} == {"0 4 * * 6", "30 2 * * 6"}

    def test_dependency_gate_excludes_unrelated_job(self, reg, fixtures_dir):
        """web-ui's cache-warm fires at 02:30 -- inside the window -- but has no
        Artifactory dependency. Reporting it would be a false positive, and false
        positives are what kill adoption of tools like this."""
        res = outcome_of(results_for(reg, EVENT, "web-ui", fixtures_dir),
                         "jobs-in-window")
        assert res.outcome is Outcome.CLEAR

    def test_cron_outside_window_not_flagged(self, source_stub, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "noon.yml").write_text(
            "name: noon\non:\n  schedule:\n    - cron: '0 12 * * *'\n"
            "jobs:\n  x:\n    runs-on: ubuntu-latest\n"
        )
        art = Artifact(
            kind="event", id="evt-window", title="t", status="active",
            source=source_stub,
            window={"start": datetime(2026, 10, 3, 2, tzinfo=UTC),
                    "end": datetime(2026, 10, 3, 6, tzinfo=UTC)},
            rules=[{"id": "jobs", "description": "d",
                    "detect": {"type": "schedule_intersects"}}],
        )
        res = evaluate_artifact(art, RepoTarget(name="t", path=tmp_path))
        assert res[0].outcome is Outcome.CLEAR

    def test_malformed_cron_is_skipped_not_crashed(self, source_stub, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "bad.yml").write_text(
            "name: bad\non:\n  schedule:\n    - cron: 'not a cron'\n"
            "jobs:\n  x:\n    runs-on: ubuntu-latest\n"
        )
        art = Artifact(
            kind="event", id="evt-bad", title="t", status="active",
            source=source_stub,
            window={"start": datetime(2026, 10, 3, 2, tzinfo=UTC),
                    "end": datetime(2026, 10, 3, 6, tzinfo=UTC)},
            rules=[{"id": "jobs", "description": "d",
                    "detect": {"type": "schedule_intersects"}}],
        )
        res = evaluate_artifact(art, RepoTarget(name="t", path=tmp_path))
        assert res[0].outcome is Outcome.CLEAR

    def test_schedule_detect_on_policy_kind_is_error(self, source_stub):
        """schedule_intersects has no meaning without a window."""
        art = Artifact(
            kind="policy", id="misuse", title="t", status="active",
            source=source_stub,
            rules=[{"id": "jobs", "description": "d",
                    "detect": {"type": "schedule_intersects"}}],
        )
        res = evaluate_artifact(art, RepoTarget(name="t", path="."))
        assert res[0].outcome is Outcome.ERROR
        assert "requires an artifact window" in res[0].error


class TestScoping:
    def test_excluded_repo_is_not_evaluated(self, reg, fixtures_dir):
        art = reg.by_id(POLICY).model_copy(update={"exclude_repos": ["payments-api"]})
        assert evaluate_artifact(
            art, RepoTarget(name="payments-api", path=fixtures_dir / "payments-api")
        ) == []
