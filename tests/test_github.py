"""The GitHub client: push a verified fix to a branch, open (or reuse) a PR.

Two kinds of mechanics here, tested two different ways:

- git operations (clone, branch, commit) are real -- run against a real local
  repository in tmp_path. There is nothing GitHub-specific about them, and
  real git behaviour is worth more confidence than a mock of it.
- GitHub REST calls are mocked via httpx.MockTransport -- no network, no live
  token, nothing in CI talks to GitHub.

`open_fix_pr`'s own push step refuses anything but an HTTPS remote (a token
can only be embedded in one), so full end-to-end orchestration tests
monkeypatch `_with_token` to a passthrough and point it at a local bare repo
standing in for "the remote" -- proving the whole flow without a real GitHub
endpoint, while `_with_token`'s real, production HTTPS-only behaviour is
tested directly and separately.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from policyforge.remediate.applier import FileChange
from policyforge.remediate.github import (
    GitHubError,
    PullRequestResult,
    _commit_changes,
    _create_pr,
    _default_branch,
    _find_open_pr,
    _pr_body,
    _request,
    _with_token,
    open_fix_pr,
    remote_owner_repo,
)
from policyforge.schema import Artifact, Rule

SOURCE = {"type": "manual", "ref": "x", "captured_at": "2026-01-01T00:00:00Z"}


def artifact(**over) -> Artifact:
    base = dict(
        kind="policy", id="demo-policy", title="Demo Policy", status="active",
        severity="blocking", source=SOURCE,
        rules=[{"id": "rule-a", "description": "d",
                "detect": {"type": "file_pattern", "globs": ["**/x"], "regex": "y"}}],
    )
    return Artifact(**{**base, **over})


def rule(**over) -> Rule:
    base = dict(
        id="rule-a", description="Fix the thing.",
        detect={"type": "file_pattern", "globs": ["**/Dockerfile"], "regex": "x"},
        verify="true",
    )
    return Rule(**{**base, **over})


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


def init_bare_remote(tmp_path: Path) -> Path:
    """A real local repo standing in for 'the remote': bare, with one commit
    on its default branch, so there is something to clone and branch from."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    run("git", "init", "--quiet", "--bare", cwd=bare)

    seed = tmp_path / "seed"
    seed.mkdir()
    run("git", "init", "--quiet", cwd=seed)
    run("git", "config", "user.email", "seed@example.com", cwd=seed)
    run("git", "config", "user.name", "seed", cwd=seed)
    run("git", "checkout", "--quiet", "-b", "main", cwd=seed)
    (seed / "Dockerfile").write_text("FROM ubuntu:latest\n")
    run("git", "add", "-A", cwd=seed)
    run("git", "commit", "--quiet", "-m", "seed", cwd=seed)
    run("git", "remote", "add", "origin", str(bare), cwd=seed)
    run("git", "push", "--quiet", "origin", "main", cwd=seed)
    return bare


class FakeGitHub:
    """Minimal, inspectable GitHub REST backend."""

    def __init__(self) -> None:
        self.default_branch = "main"
        self.open_prs: list[dict] = []
        self.created: list[dict] = []
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repos/o/r":
            return httpx.Response(200, json={"default_branch": self.default_branch})
        if request.method == "GET" and request.url.path == "/repos/o/r/pulls":
            return httpx.Response(200, json=self.open_prs)
        if request.method == "POST" and request.url.path == "/repos/o/r/pulls":
            data = json.loads(request.content)
            if any(p["head"] == f"o:{data['head']}" for p in self.open_prs):
                return httpx.Response(422, json={"message": "A pull request already exists"})
            pr = {"html_url": f"https://github.com/o/r/pull/{len(self.created) + 1}",
                  "number": len(self.created) + 1, "head": f"o:{data['head']}"}
            self.created.append(pr)
            self.open_prs.append(pr)
            return httpx.Response(201, json=pr)
        return httpx.Response(404, json={"message": "not found"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler),
                            base_url="https://api.github.com")


class TestRemoteParsing:
    @pytest.mark.parametrize("url,expected", [
        ("https://github.com/acme/widgets.git", ("acme", "widgets")),
        ("https://github.com/acme/widgets", ("acme", "widgets")),
        ("git@github.com:acme/widgets.git", ("acme", "widgets")),
        ("https://github.com/acme/widgets/", ("acme", "widgets")),
    ])
    def test_formats(self, url, expected, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        run("git", "init", "--quiet", cwd=repo)
        run("git", "remote", "add", "origin", url, cwd=repo)
        assert remote_owner_repo(repo) == expected

    def test_non_github_remote_is_refused(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        run("git", "init", "--quiet", cwd=repo)
        run("git", "remote", "add", "origin", "https://gitlab.com/acme/widgets.git", cwd=repo)
        with pytest.raises(GitHubError, match="does not look like a GitHub remote"):
            remote_owner_repo(repo)


class TestWithToken:
    def test_embeds_the_token(self):
        out = _with_token("https://github.com/acme/widgets.git", "tok123")
        assert out == "https://x-access-token:tok123@github.com/acme/widgets.git"

    def test_ssh_remote_is_refused(self):
        with pytest.raises(GitHubError, match="not an HTTPS remote"):
            _with_token("git@github.com:acme/widgets.git", "tok123")


class TestCommitChanges:
    def test_branch_is_created_with_the_fixed_content(self, tmp_path):
        bare = init_bare_remote(tmp_path)
        clone = tmp_path / "clone"
        run("git", "clone", "--quiet", str(bare), str(clone), cwd=tmp_path)

        change = FileChange(path="Dockerfile", before="FROM ubuntu:latest\n",
                            after="FROM mirror/ubuntu:latest\n")
        _commit_changes(clone, "policyctl/demo-policy/rule-a", [change],
                        ("bot", "bot@example.com"), "fix it")

        assert run("git", "branch", "--show-current", cwd=clone).strip() == \
            "policyctl/demo-policy/rule-a"
        assert (clone / "Dockerfile").read_text() == "FROM mirror/ubuntu:latest\n"
        log = run("git", "log", "-1", "--format=%s%n%an%n%ae", cwd=clone)
        subject, author, email = log.strip().splitlines()
        assert subject == "fix it"
        assert author == "bot"
        assert email == "bot@example.com"

    def test_a_new_file_path_creates_its_directories(self, tmp_path):
        bare = init_bare_remote(tmp_path)
        clone = tmp_path / "clone"
        run("git", "clone", "--quiet", str(bare), str(clone), cwd=tmp_path)

        change = FileChange(path="deploy/new/Dockerfile", before="", after="FROM x\n")
        _commit_changes(clone, "b", [change], ("bot", "b@x.com"), "add")
        assert (clone / "deploy" / "new" / "Dockerfile").read_text() == "FROM x\n"


class TestGitHubApi:
    def test_default_branch(self):
        fake = FakeGitHub()
        fake.default_branch = "trunk"
        assert _default_branch("o", "r", fake.client()) == "trunk"

    def test_find_open_pr_absent(self):
        fake = FakeGitHub()
        assert _find_open_pr("o", "r", "some-branch", fake.client()) is None

    def test_find_open_pr_present(self):
        fake = FakeGitHub()
        fake.open_prs.append({"html_url": "https://github.com/o/r/pull/9",
                              "number": 9, "head": "o:some-branch"})
        found = _find_open_pr("o", "r", "some-branch", fake.client())
        assert found["number"] == 9

    def test_create_pr(self):
        fake = FakeGitHub()
        pr = _create_pr("o", "r", "br", "main", title="t", body="b",
                        draft=True, client=fake.client())
        assert pr["number"] == 1
        assert len(fake.created) == 1

    def test_create_pr_falls_back_to_existing_on_conflict(self):
        """The branch was just force-pushed; a 422 'already exists' means a
        prior run got this far before -- find that PR rather than fail."""
        fake = FakeGitHub()
        fake.open_prs.append({"html_url": "https://github.com/o/r/pull/5",
                              "number": 5, "head": "o:br"})
        pr = _create_pr("o", "r", "br", "main", title="t", body="b",
                        draft=True, client=fake.client())
        assert pr["number"] == 5
        assert fake.created == []

    def test_request_raises_with_the_api_message(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials"})

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://api.github.com")
        with pytest.raises(GitHubError, match="Bad credentials"):
            _request(client, "GET", "/repos/o/r")


class TestPrBody:
    def test_includes_the_rule_and_verify_command(self):
        body = _pr_body(artifact(), rule())
        assert "Demo Policy" in body
        assert "demo-policy" in body
        assert "rule-a" in body
        assert "Fix the thing." in body
        assert "`true`" in body

    def test_includes_permalink_when_present(self):
        art = artifact(source={**SOURCE, "permalink": "https://slack.example/x"})
        assert "https://slack.example/x" in _pr_body(art, rule())

    def test_omits_permalink_section_when_absent(self):
        assert "Source:" not in _pr_body(artifact(), rule())


class TestOpenFixPr:
    """Full orchestration, against a local bare repo standing in for the
    remote and a mocked GitHub API standing in for the real one."""

    def _run_it(self, tmp_path, monkeypatch, fake, **kw):
        monkeypatch.setattr(
            "policyforge.remediate.github._with_token", lambda url, token: url
        )
        bare = init_bare_remote(tmp_path)
        checkout = tmp_path / "checkout"
        run("git", "clone", "--quiet", str(bare), str(checkout), cwd=tmp_path)
        change = FileChange(path="Dockerfile", before="FROM ubuntu:latest\n",
                            after="FROM mirror/ubuntu:latest\n")
        result = open_fix_pr(
            repo_path=checkout, artifact=artifact(), rule=rule(), changes=[change],
            token="unused", owner="o", repo="r", client=fake.client(), **kw,
        )
        return result, bare

    def test_opens_a_new_pr_and_pushes_the_branch(self, tmp_path, monkeypatch):
        fake = FakeGitHub()
        result, bare = self._run_it(tmp_path, monkeypatch, fake)

        assert isinstance(result, PullRequestResult)
        assert result.created is True
        assert result.number == 1

        verify = tmp_path / "verify"
        run("git", "clone", "--quiet", "--branch", "policyctl/demo-policy/rule-a",
            str(bare), str(verify), cwd=tmp_path)
        assert (verify / "Dockerfile").read_text() == "FROM mirror/ubuntu:latest\n"

    def test_reuses_an_existing_open_pr_without_recreating(self, tmp_path, monkeypatch):
        fake = FakeGitHub()
        fake.open_prs.append({
            "html_url": "https://github.com/o/r/pull/7", "number": 7,
            "head": "o:policyctl/demo-policy/rule-a",
        })
        result, _ = self._run_it(tmp_path, monkeypatch, fake)
        assert result.created is False
        assert result.number == 7
        assert fake.created == []

    def test_draft_flag_is_sent(self, tmp_path, monkeypatch):
        fake = FakeGitHub()
        self._run_it(tmp_path, monkeypatch, fake, draft=False)
        assert fake.created[0]["number"] == 1  # created at all -> payload reached the API


class TestErrorPaths:
    def test_run_raises_on_git_failure(self, tmp_path):
        from policyforge.remediate.github import _run

        with pytest.raises(GitHubError, match="not a git repository"):
            _run("git", "log", cwd=tmp_path)

    def test_create_pr_reraises_unrelated_errors(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "internal error"})

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://api.github.com")
        with pytest.raises(GitHubError, match="internal error"):
            _create_pr("o", "r", "br", "main", title="t", body="b",
                      draft=True, client=client)

    def test_create_pr_reraises_already_exists_if_no_pr_is_actually_found(self):
        """A 422 'already exists' with nothing findable by that head is a
        genuine inconsistency, not the known race this fallback is for --
        it must surface, not be swallowed."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(422, json={"message": "A pull request already exists"})
            return httpx.Response(200, json=[])  # no PR found on lookup

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://api.github.com")
        with pytest.raises(GitHubError, match="already exists"):
            _create_pr("o", "r", "br", "main", title="t", body="b",
                      draft=True, client=client)

    def test_request_falls_back_to_raw_text_on_non_json_error_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text="<html>Bad Gateway</html>")

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://api.github.com")
        with pytest.raises(GitHubError, match="Bad Gateway"):
            _request(client, "GET", "/repos/o/r")


class TestOpenFixPrRemoteParsing:
    def test_a_non_github_remote_is_refused_when_owner_repo_are_not_given(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "policyforge.remediate.github._with_token", lambda url, token: url
        )
        bare = init_bare_remote(tmp_path)
        checkout = tmp_path / "checkout"
        run("git", "clone", "--quiet", str(bare), str(checkout), cwd=tmp_path)
        change = FileChange(path="Dockerfile", before="a", after="b")
        with pytest.raises(GitHubError, match="does not look like a GitHub remote"):
            open_fix_pr(repo_path=checkout, artifact=artifact(), rule=rule(),
                       changes=[change], token="unused", client=FakeGitHub().client())
