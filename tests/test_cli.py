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
        assert "2026-10-03 03:00" in res.stdout

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
