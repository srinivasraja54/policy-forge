"""Remediation: Autofix + Finding -> FileChange, then `verify` as the oracle.

Run largely against the real `registry/` and `fixtures/repos/` -- the same
policy that ships in this repo, the same Dockerfiles `policyctl check` already
flags -- so a change to either does not quietly go untested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from policyforge.detect.base import Finding, RepoTarget
from policyforge.registry import Registry
from policyforge.remediate import RemediationError, plan_fix, verify_in_sandbox
from policyforge.schema import FilePatternDetect, Rule, ScheduleIntersectsDetect

UTC_REPOS = Path(__file__).resolve().parents[1] / "fixtures" / "repos"


def rule(**over) -> Rule:
    base = dict(
        id="rule-a",
        description="d",
        detect={"type": "file_pattern", "globs": ["**/Dockerfile"], "regex": "x"},
    )
    return Rule(**{**base, **over})


def target(name: str) -> RepoTarget:
    return RepoTarget(name=name, path=UTC_REPOS / name)


def finding(file: str, line: int = 1) -> Finding:
    return Finding(file=file, line=line, excerpt="")


class TestRuleLevelRefusals:
    """Properties of the rule, not of any one finding -- nothing is attempted."""

    def test_no_autofix_is_refused(self):
        with pytest.raises(RemediationError, match="no autofix"):
            plan_fix(rule(), target("payments-api"), [finding("Dockerfile")])

    def test_manual_autofix_is_refused(self):
        r = rule(autofix={"type": "manual", "instructions": "do it by hand"})
        with pytest.raises(RemediationError, match="manual-only"):
            plan_fix(r, target("payments-api"), [finding("Dockerfile")])

    def test_schedule_intersects_detector_is_refused(self):
        r = rule(detect={"type": "schedule_intersects"},
                 autofix={"type": "shift_schedule", "by_hours": 6})
        with pytest.raises(RemediationError, match="schedule_intersects"):
            plan_fix(r, target("data-platform"), [finding("x.yml")])

    def test_absence_rule_is_refused(self):
        r = rule(
            detect={"type": "file_pattern", "globs": ["**/*.yaml"],
                    "regex": "runAsNonRoot", "absent": True},
            autofix={"type": "manual", "instructions": "x"},
        )
        # Manual is checked first; use a mechanical autofix to reach the
        # absence check specifically.
        r = rule(
            detect={"type": "file_pattern", "globs": ["**/*.yaml"],
                    "regex": "runAsNonRoot", "absent": True},
            autofix={"type": "prefix_rewrite", "to": "x"},
        )
        with pytest.raises(RemediationError, match="absence rule"):
            plan_fix(r, target("payments-api"), [finding("x.yaml")])


class TestPrefixRewrite:
    """Against the real `registry-redirect` rule and the real fixture repos."""

    def _registry_redirect(self) -> Rule:
        art = Registry.load(Path(__file__).resolve().parents[1] / "registry").by_id(
            "artifactory-docker-passthrough-2027-01"
        )
        return next(r for r in art.rules if r.id == "registry-redirect")

    def test_a_plain_tag_is_prefixed(self):
        changes, skipped = plan_fix(
            self._registry_redirect(), target("payments-api"),
            [finding("Dockerfile")],
        )
        assert skipped == []
        assert len(changes) == 1
        assert changes[0].after.splitlines()[0] == (
            "FROM artifactory.corp/docker-remote/ubuntu:latest"
        )
        assert changes[0].changed

    def test_original_file_on_disk_is_untouched(self):
        """plan_fix computes; it never writes."""
        before = (UTC_REPOS / "payments-api" / "Dockerfile").read_text()
        plan_fix(self._registry_redirect(), target("payments-api"),
                 [finding("Dockerfile")])
        assert (UTC_REPOS / "payments-api" / "Dockerfile").read_text() == before

    def test_a_build_variable_is_skipped_not_mis_fixed(self):
        """The exact case the README calls out: `FROM $BASE_IMAGE` must not
        become `FROM artifactory.corp/docker-remote/$BASE_IMAGE`."""
        changes, skipped = plan_fix(
            self._registry_redirect(), target("payments-api"),
            [finding("services/ml-trainer/Dockerfile")],
        )
        assert changes == []
        assert len(skipped) == 1
        assert skipped[0].file == "services/ml-trainer/Dockerfile"
        assert "variable" in skipped[0].reason

    def test_a_mixed_batch_fixes_what_it_safely_can(self):
        """One tricky file must not block an otherwise-fixable one."""
        changes, skipped = plan_fix(
            self._registry_redirect(), target("payments-api"),
            [finding("Dockerfile"), finding("services/ml-trainer/Dockerfile")],
        )
        assert [c.path for c in changes] == ["Dockerfile"]
        assert [s.file for s in skipped] == ["services/ml-trainer/Dockerfile"]

    def test_an_already_compliant_reference_is_not_a_finding_so_not_reached(self):
        """web-ui's Dockerfile already points at the mirror; policyctl check
        would never produce a Finding for it, so this is just documentation of
        that boundary, not a case plan_fix itself has to handle specially."""
        content = (UTC_REPOS / "web-ui" / "Dockerfile").read_text()
        assert "artifactory.corp/docker-remote/" in content.splitlines()[0]


class TestPrefixRewriteSelfCheck:
    """The zero-cost oracle: whatever the autofix did, the detector's own
    regex must no longer match the result."""

    def test_a_fix_that_does_not_clear_its_own_detector_is_skipped(self, tmp_path):
        """Same bucket as a variable reference, not a harder failure: one
        mispaired rule should not behave differently from one awkward file."""
        (tmp_path / "Dockerfile").write_text("FROM ubuntu:latest\n")
        r = rule(
            detect={"type": "file_pattern", "globs": ["**/Dockerfile"],
                    "regex": r"^FROM\s+ubuntu"},
            # Prepending here cannot possibly clear a detector matching
            # "FROM ubuntu" literally -- a deliberately broken pairing.
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
        )
        t = RepoTarget(name="t", path=tmp_path)
        changes, skipped = plan_fix(r, t, [finding("Dockerfile")])
        assert changes == []
        assert len(skipped) == 1
        assert "did not clear" in skipped[0].reason


class TestRegexReplace:
    def test_pattern_is_rewritten_with_replacement(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ubuntu:latest\n")
        r = rule(
            detect={"type": "file_pattern", "globs": ["**/Dockerfile"],
                    "regex": r":latest\s*$"},
            autofix={"type": "regex_replace", "pattern": r":latest",
                     "replacement": "@sha256:deadbeef"},
        )
        changes, skipped = plan_fix(r, RepoTarget(name="t", path=tmp_path),
                                    [finding("Dockerfile")])
        assert skipped == []
        # regex_replace truly replaces the matched span; it does not append
        # after it -- ":latest" is gone, not left alongside the digest.
        assert changes[0].after == "FROM ubuntu@sha256:deadbeef\n"

    def test_a_fix_that_does_not_clear_its_own_detector_is_skipped(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ubuntu:latest\n")
        r = rule(
            detect={"type": "file_pattern", "globs": ["**/Dockerfile"],
                    "regex": r":latest"},
            # Replaces the wrong substring; ":latest" still matches afterwards.
            autofix={"type": "regex_replace", "pattern": "ubuntu",
                     "replacement": "debian"},
        )
        changes, skipped = plan_fix(
            r, RepoTarget(name="t", path=tmp_path), [finding("Dockerfile")]
        )
        assert changes == []
        assert "did not clear" in skipped[0].reason


class TestFileChange:
    def test_unified_diff_shows_the_line_level_change(self):
        from policyforge.remediate.applier import FileChange

        fc = FileChange(path="Dockerfile", before="FROM a\n", after="FROM b\n")
        diff = fc.unified_diff()
        assert "-FROM a" in diff
        assert "+FROM b" in diff

    def test_unchanged_is_detected(self):
        from policyforge.remediate.applier import FileChange

        assert FileChange(path="x", before="a", after="a").changed is False


class TestVerifyInSandbox:
    def test_a_passing_command_passes(self, tmp_path):
        (tmp_path / "f.txt").write_text("hello\n")
        from policyforge.remediate.applier import FileChange

        result = verify_in_sandbox(tmp_path, [], "true")
        assert result.passed is True
        assert result.returncode == 0

    def test_a_failing_command_fails(self, tmp_path):
        result = verify_in_sandbox(tmp_path, [], "exit 1")
        assert result.passed is False
        assert result.returncode == 1

    def test_a_timeout_is_reported_not_raised(self, tmp_path):
        result = verify_in_sandbox(tmp_path, [], "sleep 5", timeout=1)
        assert result.passed is False
        assert result.timed_out is True

    def test_changes_are_visible_to_the_command(self, tmp_path):
        from policyforge.remediate.applier import FileChange

        (tmp_path / "Dockerfile").write_text("FROM ubuntu:latest\n")
        change = FileChange(
            path="Dockerfile", before="FROM ubuntu:latest\n",
            after="FROM artifactory.corp/docker-remote/ubuntu:latest\n",
        )
        result = verify_in_sandbox(
            tmp_path, [change], "grep -q artifactory.corp Dockerfile"
        )
        assert result.passed is True

    def test_the_real_checkout_is_never_modified(self, tmp_path):
        from policyforge.remediate.applier import FileChange

        (tmp_path / "Dockerfile").write_text("FROM ubuntu:latest\n")
        change = FileChange(
            path="Dockerfile", before="FROM ubuntu:latest\n",
            after="FROM mirror/ubuntu:latest\n",
        )
        verify_in_sandbox(tmp_path, [change], "true")
        assert (tmp_path / "Dockerfile").read_text() == "FROM ubuntu:latest\n"

    def test_stdout_and_stderr_are_captured(self, tmp_path):
        result = verify_in_sandbox(tmp_path, [], "echo out; echo err >&2; exit 1")
        assert "out" in result.stdout
        assert "err" in result.stderr
        assert result.passed is False


class TestPerFileFailureIsolation:
    """Every way plan_fix can fail on one file without aborting the whole call."""

    def test_an_unreadable_file_is_skipped(self, tmp_path):
        # A directory where a file is expected: read_text() raises IsADirectoryError.
        (tmp_path / "Dockerfile").mkdir()
        r = rule(autofix={"type": "prefix_rewrite", "to": "mirror/"})
        changes, skipped = plan_fix(r, RepoTarget(name="t", path=tmp_path),
                                    [finding("Dockerfile")])
        assert changes == []
        assert "cannot read" in skipped[0].reason

    def test_shift_schedule_paired_with_file_pattern_is_skipped(self, tmp_path):
        """Schema does not forbid this odd pairing; plan_fix must not crash on
        it, and must not confuse it with a genuine schedule_intersects rule."""
        (tmp_path / "Dockerfile").write_text("FROM ubuntu:latest\n")
        r = rule(autofix={"type": "shift_schedule", "by_hours": 6})
        changes, skipped = plan_fix(r, RepoTarget(name="t", path=tmp_path),
                                    [finding("Dockerfile")])
        assert changes == []
        assert "not reachable" in skipped[0].reason


class TestApplyDirectly:
    """The catch-all in `_apply`: defensive, unreachable through plan_fix
    (which already filters ManualFix, and the schema has no fifth Autofix
    variant), kept because a future Autofix variant should fail loudly here
    rather than fall through silently."""

    def test_an_unknown_autofix_type_is_refused(self):
        from policyforge.remediate.applier import _apply

        class _NotAnAutofix:
            pass

        with pytest.raises(RemediationError, match="unsupported autofix type"):
            _apply("x", _NotAnAutofix(), "text", file="f")


class TestDecode:
    def test_none_becomes_empty_string(self):
        from policyforge.remediate.verify import _decode

        assert _decode(None) == ""

    def test_bytes_are_decoded(self):
        from policyforge.remediate.verify import _decode

        assert _decode(b"hello") == "hello"

    def test_str_passes_through(self):
        from policyforge.remediate.verify import _decode

        assert _decode("hello") == "hello"
