"""Search-backend resolution and cross-backend parity.

Two backends implement the same rule semantics. They must agree, and which one
ran must be observable. These tests exist because `rg` is frequently a shell
function or alias rather than an executable -- in that case shutil.which finds
nothing, and without this coverage the project would silently run the slow path
while its documentation claimed otherwise.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from policyforge.detect import RepoTarget, evaluate_artifact, file_pattern
from policyforge.detect.base import Outcome
from policyforge.registry import Registry

POLICY = "artifactory-docker-passthrough-2027-01"
REAL_RG = shutil.which("rg")
needs_rg = pytest.mark.skipif(REAL_RG is None, reason="no ripgrep binary on PATH")


class TestResolution:
    def test_backend_name_matches_rg_presence(self, monkeypatch):
        monkeypatch.setattr(file_pattern, "RG", "/somewhere/rg")
        assert file_pattern.backend() == "ripgrep"
        monkeypatch.setattr(file_pattern, "RG", None)
        assert file_pattern.backend() == "python"

    def test_override_env_is_honoured(self, monkeypatch, tmp_path):
        fake = tmp_path / "rg"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("POLICYFORGE_RG", str(fake))
        assert file_pattern._resolve_rg() == str(fake)

    def test_override_pointing_at_nothing_falls_back(self, monkeypatch, tmp_path):
        """A broken override must degrade to the Python backend, not crash."""
        monkeypatch.setenv("POLICYFORGE_RG", str(tmp_path / "does-not-exist"))
        assert file_pattern._resolve_rg() is None

    def test_override_pointing_at_non_executable_falls_back(self, monkeypatch, tmp_path):
        plain = tmp_path / "rg"
        plain.write_text("not executable")
        monkeypatch.setenv("POLICYFORGE_RG", str(plain))
        assert file_pattern._resolve_rg() is None


class TestErrorPropagation:
    def test_failing_binary_is_error_not_clear(self, monkeypatch, tmp_path, registry_dir):
        """If the search tool fails, the repo must read as ERROR. Treating a tool
        failure as 'no matches found' would report broken scans as compliance --
        the single most dangerous failure mode in a tool like this."""
        broken = tmp_path / "rg"
        broken.write_text("#!/bin/sh\necho 'boom' >&2\nexit 2\n")
        broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setattr(file_pattern, "RG", str(broken))

        art = Registry.load(registry_dir).by_id(POLICY)
        res = evaluate_artifact(art, RepoTarget(name="x", path=tmp_path))[0]
        assert res.outcome is Outcome.ERROR
        assert "ripgrep failed" in res.error

    def test_binary_reporting_no_matches_is_clear(self, monkeypatch, tmp_path, registry_dir):
        """rg exit code 1 means 'no matches', which is a clean pass, not an error."""
        quiet = tmp_path / "rg"
        quiet.write_text("#!/bin/sh\nexit 1\n")
        quiet.chmod(quiet.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setattr(file_pattern, "RG", str(quiet))

        art = Registry.load(registry_dir).by_id(POLICY)
        res = evaluate_artifact(art, RepoTarget(name="x", path=tmp_path))[0]
        assert res.outcome is Outcome.CLEAR


@needs_rg
class TestParityWithRealRipgrep:
    """Runs only where a genuine ripgrep binary exists. On machines without one
    these skip rather than silently passing on the Python path."""

    @pytest.fixture
    def both_backends(self, registry_dir, fixtures_dir, monkeypatch):
        def run(repo: str, rule: str, rg: str | None):
            monkeypatch.setattr(file_pattern, "RG", rg)
            art = Registry.load(registry_dir).by_id(POLICY)
            results = evaluate_artifact(
                art, RepoTarget(name=repo, path=fixtures_dir / repo)
            )
            return next(r for r in results if r.rule_id == rule)

        return run

    @pytest.mark.parametrize("repo", ["payments-api", "data-platform", "web-ui"])
    @pytest.mark.parametrize("rule", ["registry-redirect", "digest-pinning"])
    def test_backends_agree(self, both_backends, repo, rule):
        via_rg = both_backends(repo, rule, REAL_RG)
        via_py = both_backends(repo, rule, None)
        assert via_rg.outcome is via_py.outcome
        assert {(f.file, f.line) for f in via_rg.findings} == \
               {(f.file, f.line) for f in via_py.findings}

    def test_pcre2_lookahead_works_under_ripgrep(self, both_backends):
        """The registry-redirect rule uses a negative lookahead. Python's `re`
        supports it natively; this asserts the --pcre2 flag makes rg agree."""
        res = both_backends("web-ui", "registry-redirect", REAL_RG)
        assert res.outcome is Outcome.CLEAR


class TestRipgrepJsonParsing:
    """Covers the rg --json decoding path without requiring a real binary, by
    standing in a stub that emits genuine ripgrep event output."""

    @pytest.fixture
    def stub_rg(self, tmp_path, monkeypatch):
        def install(payload: str):
            stub = tmp_path / "rg"
            stub.write_text(f"#!/bin/sh\ncat <<'JSON'\n{payload}\nJSON\nexit 0\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            monkeypatch.setattr(file_pattern, "RG", str(stub))
            return stub

        return install

    def _run(self, registry_dir, path):
        art = Registry.load(registry_dir).by_id(POLICY)
        return evaluate_artifact(art, RepoTarget(name="x", path=path))[0]

    def test_parses_match_events(self, stub_rg, registry_dir, tmp_path):
        stub_rg(
            '{"type":"begin","data":{"path":{"text":"./Dockerfile"}}}\n'
            '{"type":"match","data":{"path":{"text":"./Dockerfile"},'
            '"lines":{"text":"FROM ubuntu:latest\\n"},"line_number":1}}\n'
            '{"type":"end","data":{"path":{"text":"./Dockerfile"}}}'
        )
        res = self._run(registry_dir, tmp_path)
        assert res.outcome is Outcome.VIOLATING
        assert res.findings[0].file == "Dockerfile"   # leading ./ stripped
        assert res.findings[0].line == 1
        assert res.findings[0].excerpt == "FROM ubuntu:latest"

    def test_ignores_non_match_events(self, stub_rg, registry_dir, tmp_path):
        stub_rg('{"type":"begin","data":{"path":{"text":"./a"}}}\n'
                '{"type":"summary","data":{"elapsed_total":{"secs":0}}}')
        assert self._run(registry_dir, tmp_path).outcome is Outcome.CLEAR

    def test_tolerates_unparseable_lines(self, stub_rg, registry_dir, tmp_path):
        """rg can interleave non-JSON on some builds; a stray line must not abort
        the whole scan and turn a real violation into a silent pass."""
        stub_rg(
            'not json at all\n'
            '{"type":"match","data":{"path":{"text":"./Dockerfile"},'
            '"lines":{"text":"FROM ubuntu:latest\\n"},"line_number":3}}'
        )
        res = self._run(registry_dir, tmp_path)
        assert res.count == 1
        assert res.findings[0].line == 3

    def test_decodes_base64_paths_for_non_utf8(self, stub_rg, registry_dir, tmp_path):
        """rg emits {"bytes": <base64>} instead of {"text": ...} for paths that
        are not valid UTF-8."""
        import base64

        encoded = base64.b64encode(b"./weird\xff/Dockerfile").decode()
        stub_rg(
            '{"type":"match","data":{"path":{"bytes":"%s"},'
            '"lines":{"text":"FROM ubuntu:latest\\n"},"line_number":1}}' % encoded
        )
        res = self._run(registry_dir, tmp_path)
        assert res.count == 1
        assert res.findings[0].file.endswith("/Dockerfile")

    def test_null_line_number_becomes_zero(self, stub_rg, registry_dir, tmp_path):
        stub_rg('{"type":"match","data":{"path":{"text":"./Dockerfile"},'
                '"lines":{"text":"FROM ubuntu:latest\\n"},"line_number":null}}')
        assert self._run(registry_dir, tmp_path).findings[0].line == 0
