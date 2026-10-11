"""CLI behaviour, with emphasis on exit codes.

`policyctl check` is wired up as a required status check, so its exit code is a
load-bearing interface, not cosmetic output. Exit 1 must mean "a blocking
obligation is past its deadline and violated" and nothing else.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from policyforge.cli import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, repo_root: Path) -> Path:
    """An isolated copy of the registry and fixtures, safe to mutate."""
    shutil.copytree(repo_root / "registry", tmp_path / "registry")
    shutil.copytree(repo_root / "fixtures", tmp_path / "fixtures")
    return tmp_path


def invoke(workspace: Path, *args: str):
    return runner.invoke(app, [*args, "--registry", str(workspace / "registry")])


def set_deadline(workspace: Path, deadline: str) -> None:
    path = workspace / "registry" / "policies" / \
        "artifactory-docker-passthrough-2027-01.json"
    data = json.loads(path.read_text())
    data["deadline"] = deadline
    path.write_text(json.dumps(data))


def only_policy(workspace: Path) -> None:
    """Drop the event artifact so tests can reason about one artifact at a time."""
    shutil.rmtree(workspace / "registry" / "events")


class TestValidate:
    def test_valid_registry_exits_zero(self, workspace):
        res = invoke(workspace, "validate")
        assert res.exit_code == 0
        assert "2 artifact(s) valid" in res.stdout

    def test_malformed_json_exits_two(self, workspace):
        (workspace / "registry" / "policies" / "broken.json").write_text("{nope")
        res = invoke(workspace, "validate")
        assert res.exit_code == 2
        assert "not valid JSON" in res.stdout

    def test_schema_violation_exits_two(self, workspace):
        (workspace / "registry" / "policies" / "bad.json").write_text(
            json.dumps({"schema_version": 1, "kind": "event", "id": "no-window",
                        "title": "t", "rules": [],
                        "source": {"type": "slack", "ref": "r",
                                   "captured_at": "2026-09-01T00:00:00Z"}})
        )
        res = invoke(workspace, "validate")
        assert res.exit_code == 2


class TestCheckExitCodes:
    def test_future_deadline_does_not_fail_build(self, workspace):
        """A blocking policy dated in the future must warn, not break CI today."""
        only_policy(workspace)
        set_deadline(workspace, "2099-01-15")
        res = invoke(workspace, "check", "--root", str(workspace), "--no-record")
        assert res.exit_code == 0
        assert "violating" in res.stdout

    def test_past_deadline_fails_build(self, workspace):
        only_policy(workspace)
        set_deadline(workspace, "2020-01-15")
        res = invoke(workspace, "check", "--root", str(workspace), "--no-record")
        assert res.exit_code == 1
        assert "BLOCKING" in res.stdout

    def test_no_fail_flag_suppresses_exit_code(self, workspace):
        """Report-only mode, for running on repos you do not yet gate."""
        only_policy(workspace)
        set_deadline(workspace, "2020-01-15")
        res = invoke(workspace, "check", "--root", str(workspace),
                     "--no-record", "--no-fail")
        assert res.exit_code == 0

    def test_clean_repo_alone_exits_zero(self, workspace):
        only_policy(workspace)
        set_deadline(workspace, "2020-01-15")
        res = invoke(workspace, "check", "--root", str(workspace),
                     "--no-record", "--repo", "web-ui")
        assert res.exit_code == 0
        assert "0 violating" in res.stdout

    def test_proposed_artifacts_are_not_enforced(self, workspace):
        """Until a human merges the registry PR, an artifact must do nothing."""
        only_policy(workspace)
        path = workspace / "registry" / "policies" / \
            "artifactory-docker-passthrough-2027-01.json"
        data = json.loads(path.read_text())
        data["status"] = "proposed"
        path.write_text(json.dumps(data))
        res = invoke(workspace, "check", "--root", str(workspace), "--no-record")
        assert res.exit_code == 0
        assert "no enforceable artifacts" in res.stdout


class TestCheckOutput:
    def test_reports_cron_intersection_detail(self, workspace):
        res = invoke(workspace, "check", "--root", str(workspace), "--no-record",
                     "--repo", "payments-api")
        assert "fires in window" in res.stdout
        assert "2037-10-03 03:00" in res.stdout

    def test_dependency_gate_keeps_web_ui_clear(self, workspace):
        res = invoke(workspace, "check", "--root", str(workspace), "--no-record",
                     "--repo", "web-ui")
        assert "0 violating" in res.stdout

    def test_records_to_ledger_when_enabled(self, workspace):
        ledger = workspace / "l.sqlite"
        invoke(workspace, "check", "--root", str(workspace), "--ledger", str(ledger))
        assert ledger.exists()
        from policyforge.ledger import Ledger
        assert len(Ledger(ledger).latest_status()) > 0

    def test_missing_repo_path_surfaces_as_error_not_pass(self, workspace):
        """A repo we could not read must never be counted as compliant."""
        shutil.rmtree(workspace / "fixtures" / "repos" / "payments-api")
        res = invoke(workspace, "check", "--root", str(workspace), "--no-record",
                     "--repo", "payments-api")
        assert "error" in res.stdout
        assert "results are incomplete" in res.stdout


class TestReportAndBrief:
    def test_report_on_empty_ledger_is_graceful(self, workspace):
        res = invoke(workspace, "report", "--ledger", str(workspace / "empty.sqlite"))
        assert res.exit_code == 0
        assert "ledger is empty" in res.stdout

    def test_report_shows_coverage_after_a_check(self, workspace):
        ledger = workspace / "l.sqlite"
        invoke(workspace, "check", "--root", str(workspace), "--ledger", str(ledger))
        res = invoke(workspace, "report", "--ledger", str(ledger))
        assert res.exit_code == 0
        assert "burndown" in res.stdout

    def test_brief_for_team_with_violations(self, workspace):
        ledger = workspace / "l.sqlite"
        invoke(workspace, "check", "--root", str(workspace), "--ledger", str(ledger))
        res = invoke(workspace, "brief", "payments", "--ledger", str(ledger))
        assert res.exit_code == 0
        assert "payments-api" in res.stdout

    def test_brief_for_clean_team_says_so(self, workspace):
        ledger = workspace / "l.sqlite"
        invoke(workspace, "check", "--root", str(workspace), "--ledger", str(ledger))
        res = invoke(workspace, "brief", "frontend", "--ledger", str(ledger))
        assert res.exit_code == 0
        assert "No open obligations" in res.stdout

    def test_unknown_team_exits_one_and_lists_known(self, workspace):
        res = invoke(workspace, "brief", "nosuchteam",
                     "--ledger", str(workspace / "l.sqlite"))
        assert res.exit_code == 1
        assert "known teams" in res.stdout


class TestCatalogHandling:
    def test_repo_without_path_is_skipped_with_warning(self, workspace):
        cat = workspace / "registry" / "catalog.yaml"
        data = yaml.safe_load(cat.read_text())
        data["repos"].append({"name": "remote-only", "team": "payments"})
        cat.write_text(yaml.safe_dump(data))
        res = invoke(workspace, "check", "--root", str(workspace), "--no-record")
        assert "no local path" in res.stdout
        assert res.exit_code == 0


class TestCompileCommand:
    """The compile command must never write an enforceable artifact, and must
    degrade usefully when no credentials are present."""

    @pytest.fixture
    def bare(self, tmp_path, repo_root):
        reg = tmp_path / "registry"
        (reg / "policies").mkdir(parents=True)
        (reg / "events").mkdir(parents=True)
        (reg / "catalog.yaml").write_text(
            (repo_root / "registry" / "catalog.yaml").read_text()
        )
        return tmp_path

    def _run(self, bare, repo_root, *extra):
        return runner.invoke(app, [
            "compile", "--stub",
            "--source", str(repo_root / "fixtures" / "slack"),
            "--registry", str(bare / "registry"),
            "--since-days", "400", *extra,
        ])

    def test_compiles_and_reports_the_funnel(self, bare, repo_root):
        res = self._run(bare, repo_root)
        assert res.exit_code == 0
        assert "3 compiled" in res.stdout
        assert "6 noise" in res.stdout
        assert "1 amendment" in res.stdout
        # Three compilations plus one amendment. The amendment uses the same
        # expensive model, so it is counted with them rather than hidden.
        assert "4 compile call(s)" in res.stdout

    def test_amended_artifacts_are_reported_apart_from_written_ones(self, bare, repo_root):
        """A new artifact is inert until promoted; an amended one may already be
        active. Reporting them in one list would flatten that difference."""
        res = self._run(bare, repo_root)
        assert "wrote 3 artifact(s)" in res.stdout
        assert "amended 1 existing artifact(s)" in res.stdout
        assert "takes effect on merge" in res.stdout

    def test_written_artifacts_are_proposed_and_inert(self, bare, repo_root):
        self._run(bare, repo_root)
        assert runner.invoke(app, ["validate", "--registry",
                                   str(bare / "registry")]).exit_code == 0
        res = runner.invoke(app, ["check", "--registry", str(bare / "registry"),
                                  "--no-record"])
        assert "no enforceable artifacts" in res.stdout

    def test_dry_run_writes_nothing(self, bare, repo_root):
        res = self._run(bare, repo_root, "--dry-run")
        assert "dry run" in res.stdout
        assert list((bare / "registry" / "policies").glob("*.json")) == []

    def test_missing_credentials_exits_two_with_a_hint(self, bare, repo_root, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(
            "policyforge.compiler.AnthropicClient",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credentials")),
        )
        res = runner.invoke(app, [
            "compile", "--source", str(repo_root / "fixtures" / "slack"),
            "--registry", str(bare / "registry"),
        ])
        assert res.exit_code == 2
        assert "--stub" in res.stdout

    def test_slack_without_config_exits_two_with_a_hint(self, bare, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CHANNEL_IDS", raising=False)
        # A real .env with real Slack credentials (as a developer actually
        # running --slack would have) would otherwise undo the two deletions
        # above: _load_dotenv() doesn't know this test wants them absent.
        monkeypatch.setattr("policyforge.cli._load_dotenv", lambda: None)
        res = runner.invoke(app, [
            "compile", "--stub", "--slack", "--registry", str(bare / "registry"),
        ])
        assert res.exit_code == 2
        assert "cannot read Slack" in res.stdout

    def test_slack_and_stub_are_independent(self, bare, monkeypatch):
        """--slack --stub reads real channel traffic through canned model
        responses -- the way to smoke-test the connection without spending."""
        from datetime import datetime, timezone

        from policyforge.schema import SourceType
        from policyforge.sources.base import Message

        class _FakeSlackSource:
            name = "slack"
            channels = ["C0TEST"]

            def __init__(self, *a, **k):
                pass

            def fetch(self, since):
                yield Message(
                    ref="C0TEST/p1", text="the coffee machine is broken",
                    posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    source_type=SourceType.SLACK,
                )

        monkeypatch.setattr("policyforge.sources.slack.SlackSource", _FakeSlackSource)
        res = runner.invoke(app, [
            "compile", "--stub", "--slack", "--registry", str(bare / "registry"),
            "--since-days", "400",
        ])
        assert res.exit_code == 0
        assert "reading Slack channel(s) C0TEST" in res.stdout
        assert "1 message(s): 0 compiled" in res.stdout


class TestRemediateCommand:
    """`policyctl remediate` writes to a real checkout, so its default must be
    inert: show what would change, apply nothing, unless --write AND verify
    both say yes."""

    def _policy(self, *, autofix: dict, verify: str | None = None) -> dict:
        return {
            "schema_version": 1, "kind": "policy", "id": "demo-policy",
            "title": "t", "status": "active", "severity": "warn",
            "source": {"type": "manual", "ref": "x",
                       "captured_at": "2026-01-01T00:00:00Z"},
            "rules": [{
                "id": "no-latest", "description": "d",
                "detect": {"type": "file_pattern", "globs": ["**/Dockerfile"],
                           "regex": r"^FROM\s+(?!mirror/)"},
                "autofix": autofix,
                **({"verify": verify} if verify else {}),
            }],
        }

    @pytest.fixture
    def demo(self, tmp_path: Path):
        """A minimal, self-contained registry + repo -- deliberately not the
        shared fixtures, so these tests do not depend on docker being
        installed or running."""
        (tmp_path / "registry" / "policies").mkdir(parents=True)
        (tmp_path / "repo").mkdir()
        (tmp_path / "registry" / "catalog.yaml").write_text(
            yaml.dump({"repos": [{"name": "demo", "path": "repo", "team": "x"}]})
        )
        (tmp_path / "repo" / "Dockerfile").write_text("FROM ubuntu:latest\n")
        return tmp_path

    def _write_policy(self, demo: Path, policy: dict) -> None:
        (demo / "registry" / "policies" / "demo-policy.json").write_text(
            json.dumps(policy)
        )

    def _run(self, demo: Path, *extra: str):
        return runner.invoke(app, [
            "remediate", "--registry", str(demo / "registry"),
            "--root", str(demo), *extra,
        ])

    def test_dry_run_shows_the_diff_and_writes_nothing(self, demo):
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="grep -q mirror/ Dockerfile",
        ))
        res = self._run(demo)
        assert res.exit_code == 0
        assert "+FROM mirror/ubuntu:latest" in res.stdout
        assert "verify passed" in res.stdout
        assert "0 written" in res.stdout
        assert (demo / "repo" / "Dockerfile").read_text() == "FROM ubuntu:latest\n"

    def test_write_applies_a_fix_that_passes_verify(self, demo):
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="grep -q mirror/ Dockerfile",
        ))
        res = self._run(demo, "--write")
        assert "1 written" in res.stdout
        assert (demo / "repo" / "Dockerfile").read_text() == "FROM mirror/ubuntu:latest\n"

    def test_write_does_not_apply_a_fix_that_fails_verify(self, demo):
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="exit 1",
        ))
        res = self._run(demo, "--write")
        assert "verify failed" in res.stdout
        assert "0 written" in res.stdout
        assert (demo / "repo" / "Dockerfile").read_text() == "FROM ubuntu:latest\n"

    def test_a_rule_with_no_verify_is_never_written_even_with_write(self, demo):
        """No flag can skip the oracle -- a computed fix with nothing to
        confirm it stays diff-only, by design, not by omission."""
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
        ))
        res = self._run(demo, "--write")
        assert "no verify command set" in res.stdout
        assert "0 written" in res.stdout
        assert (demo / "repo" / "Dockerfile").read_text() == "FROM ubuntu:latest\n"

    def test_manual_autofix_is_reported_not_attempted(self, demo):
        self._write_policy(demo, self._policy(
            autofix={"type": "manual", "instructions": "fix it by hand"},
        ))
        res = self._run(demo)
        assert "cannot autofix" in res.stdout
        assert "manual-only" in res.stdout

    def test_a_clean_repo_computes_nothing(self, demo):
        (demo / "repo" / "Dockerfile").write_text("FROM mirror/ubuntu:latest\n")
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="true",
        ))
        res = self._run(demo)
        assert "0 fix(es) computed" in res.stdout

    def test_no_enforceable_artifacts(self, demo):
        res = self._run(demo)
        assert "no enforceable artifacts" in res.stdout

    def test_unknown_repo_filter_reports_no_targets(self, demo):
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
        ))
        res = self._run(demo, "--repo", "nope")
        assert "no targets" in res.stdout

    def test_a_rule_with_no_autofix_is_silently_left_alone(self, demo):
        """Not every violating rule has an autofix -- remediate must not print
        anything for one, since there is nothing for it to propose."""
        policy = self._policy(autofix={"type": "manual", "instructions": "x"})
        del policy["rules"][0]["autofix"]
        self._write_policy(demo, policy)
        res = self._run(demo)
        assert "no-latest" not in res.stdout
        assert "0 fix(es) computed" in res.stdout

    def test_verify_failure_output_is_shown(self, demo):
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="echo something went wrong >&2; exit 1",
        ))
        res = self._run(demo, "--write")
        assert "verify failed" in res.stdout
        assert "something went wrong" in res.stdout

    # ---- --pr --------------------------------------------------------------

    def test_pr_without_github_token_exits_two(self, demo, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="grep -q mirror/ Dockerfile",
        ))
        res = self._run(demo, "--pr")
        assert res.exit_code == 2
        assert "GITHUB_TOKEN" in res.stdout

    def test_pr_opens_a_pull_request_and_records_it_in_the_ledger(self, demo, monkeypatch):
        from policyforge.ledger import Ledger
        from policyforge.remediate.github import PullRequestResult

        calls = []

        def fake_open_fix_pr(**kw):
            calls.append(kw)
            return PullRequestResult(url="https://github.com/o/r/pull/1",
                                     number=1, created=True)

        monkeypatch.setattr("policyforge.remediate.github.open_fix_pr", fake_open_fix_pr)
        monkeypatch.setenv("GITHUB_TOKEN", "tok123")
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="grep -q mirror/ Dockerfile",
        ))

        ledger_path = demo / "l.sqlite"
        res = self._run(demo, "--pr", "--ledger", str(ledger_path))

        assert res.exit_code == 0
        assert "PR opened" in res.stdout
        assert "https://github.com/o/r/pull/1" in res.stdout
        assert "1 PR(s) opened" in res.stdout
        assert len(calls) == 1
        assert calls[0]["artifact"].id == "demo-policy"
        assert calls[0]["rule"].id == "no-latest"
        assert calls[0]["token"] == "tok123"

        action = Ledger(ledger_path).open_actions()[0]
        assert action["url"] == "https://github.com/o/r/pull/1"
        assert action["kind"] == "pull_request"

    def test_pr_is_not_reopened_once_already_claimed(self, demo, monkeypatch):
        calls = []
        monkeypatch.setattr("policyforge.remediate.github.open_fix_pr",
                            lambda **kw: calls.append(kw))
        monkeypatch.setenv("GITHUB_TOKEN", "tok123")
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="grep -q mirror/ Dockerfile",
        ))

        ledger_path = demo / "l.sqlite"
        self._run(demo, "--pr", "--ledger", str(ledger_path))
        assert len(calls) == 1

        res = self._run(demo, "--pr", "--ledger", str(ledger_path))
        assert "PR already opened for this fix" in res.stdout
        assert "0 PR(s) opened" in res.stdout
        assert len(calls) == 1, "must not call open_fix_pr again"

    def test_pr_failure_is_reported_and_recorded_not_fatal(self, demo, monkeypatch):
        from policyforge.ledger import Ledger
        from policyforge.remediate.github import GitHubError

        def fail(**kw):
            raise GitHubError("401 bad credentials")

        monkeypatch.setattr("policyforge.remediate.github.open_fix_pr", fail)
        monkeypatch.setenv("GITHUB_TOKEN", "tok123")
        self._write_policy(demo, self._policy(
            autofix={"type": "prefix_rewrite", "to": "mirror/"},
            verify="grep -q mirror/ Dockerfile",
        ))

        ledger_path = demo / "l.sqlite"
        res = self._run(demo, "--pr", "--ledger", str(ledger_path))

        assert res.exit_code == 0
        assert "could not open a PR" in res.stdout
        assert "bad credentials" in res.stdout
        assert "0 PR(s) opened" in res.stdout

        action = Ledger(ledger_path).open_actions()
        assert action == [], "a failed attempt must not be left looking open"
