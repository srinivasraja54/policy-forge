"""Opening a verified fix as a GitHub pull request.

The last step the project's own thesis describes: the fix was computed
mechanically (`applier.py`), confirmed by the rule's own `verify` command in a
sandbox (`verify.py`); this module is what turns that into something a human
can actually review and merge, rather than a diff that lives only in a
terminal.

Git operations (branch, commit, push) run in a throwaway clone -- never the
caller's real checkout -- the same principle `verify_in_sandbox` already
applies for a different reason: a push that goes wrong must leave no trace on
a repo a human has open, and nothing here should care what branch the local
checkout happened to be on when this ran.

Opening the pull request is the only network call that needs the GitHub REST
API directly; everything about the fix itself is already decided by the time
this module is reached.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..schema import Artifact, Rule
from .applier import FileChange

_API = "https://api.github.com"
_REMOTE_RE = re.compile(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$")


class GitHubError(Exception):
    """A git operation or GitHub API call failed outright."""


@dataclass(frozen=True)
class PullRequestResult:
    url: str
    number: int
    created: bool  # False when an existing open PR was found and reused


def remote_owner_repo(repo_path: Path) -> tuple[str, str]:
    """`owner, repo` parsed from the checkout's own `origin` remote.

    Reading it from the checkout rather than a new catalog field is
    deliberate: a real deployment already has the repo checked out (CI's
    `actions/checkout`, or a local clone) specifically to run `remediate`
    against, and that checkout's remote is the one place this cannot drift
    from where the fix actually needs to land.
    """
    url = _run("git", "remote", "get-url", "origin", cwd=repo_path).strip()
    m = _REMOTE_RE.search(url)
    if not m:
        raise GitHubError(f"'{url}' does not look like a GitHub remote")
    return m.group(1), m.group(2)


def open_fix_pr(
    *,
    repo_path: Path,
    artifact: Artifact,
    rule: Rule,
    changes: list[FileChange],
    token: str,
    owner: str | None = None,
    repo: str | None = None,
    client: httpx.Client | None = None,
    draft: bool = True,
    git_author: tuple[str, str] = ("policy-forge[bot]", "policy-forge@users.noreply.github.com"),
) -> PullRequestResult:
    """Push `changes` to a dedicated branch and open (or reuse) a PR for it.

    `owner`/`repo` default to whatever `repo_path`'s own `origin` remote
    resolves to; passing them explicitly is mainly for tests, where the
    checkout's actual remote is a throwaway local repo, not GitHub.

    The branch is `policyctl/<artifact.id>/<rule.id>` -- one branch per rule,
    force-pushed on every call. That branch is understood to be owned
    entirely by this tool: nothing else should ever commit to it, so
    overwriting it with the latest computed fix is safe where force-pushing a
    human's own branch would not be.
    """
    remote_url = _run("git", "remote", "get-url", "origin", cwd=repo_path).strip()
    if owner is None or repo is None:
        m = _REMOTE_RE.search(remote_url)
        if not m:
            raise GitHubError(f"'{remote_url}' does not look like a GitHub remote")
        owner, repo = m.group(1), m.group(2)

    client = client or httpx.Client(
        base_url=_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )

    branch = f"policyctl/{artifact.id}/{rule.id}"
    default_branch = _default_branch(owner, repo, client)

    _push_branch(remote_url, token, branch, changes, git_author,
                 subject=f"policyctl remediate: {rule.description}"[:72])

    existing = _find_open_pr(owner, repo, branch, client)
    if existing is not None:
        return PullRequestResult(url=existing["html_url"], number=existing["number"],
                                 created=False)

    body = _pr_body(artifact, rule)
    pr = _create_pr(owner, repo, branch, default_branch,
                    title=f"{artifact.title} — {rule.id}", body=body,
                    draft=draft, client=client)
    return PullRequestResult(url=pr["html_url"], number=pr["number"], created=True)


# ---- git mechanics, in a throwaway clone -----------------------------------


def _push_branch(
    remote_url: str,
    token: str,
    branch: str,
    changes: list[FileChange],
    git_author: tuple[str, str],
    *,
    subject: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="policyforge-pr-") as tmp:
        clone = Path(tmp) / "repo"
        _run("git", "clone", "--quiet", remote_url, str(clone))
        _commit_changes(clone, branch, changes, git_author, subject)
        push_url = _with_token(remote_url, token)
        _run("git", "-C", str(clone), "push", "--quiet", "--force",
             push_url, f"HEAD:{branch}")


def _commit_changes(
    clone: Path,
    branch: str,
    changes: list[FileChange],
    git_author: tuple[str, str],
    subject: str,
) -> None:
    """Everything up to (not including) the push: a fresh branch, the file
    changes written and committed. Separate from `_push_branch` so the commit
    mechanics can be tested against a real local clone without needing a real
    HTTPS remote to push to -- `_with_token` refuses anything else."""
    name, email = git_author
    _run("git", "-C", str(clone), "checkout", "-b", branch)
    _run("git", "-C", str(clone), "config", "user.name", name)
    _run("git", "-C", str(clone), "config", "user.email", email)
    for change in changes:
        path = clone / change.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(change.after)
    _run("git", "-C", str(clone), "add", "-A")
    _run("git", "-C", str(clone), "commit", "--quiet", "-m", subject)


def _with_token(remote_url: str, token: str) -> str:
    """An HTTPS remote URL with the token embedded, for a one-shot push that
    never writes the token into the clone's own git config."""
    if not remote_url.startswith("https://"):
        raise GitHubError(
            f"'{remote_url}' is not an HTTPS remote -- a token can only be "
            "embedded in an HTTPS push URL, not SSH"
        )
    return remote_url.replace("https://", f"https://x-access-token:{token}@", 1)


def _run(*args: str, cwd: str | Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitHubError(f"{' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


# ---- GitHub REST API --------------------------------------------------------


def _default_branch(owner: str, repo: str, client: httpx.Client) -> str:
    return _request(client, "GET", f"/repos/{owner}/{repo}")["default_branch"]


def _find_open_pr(owner: str, repo: str, branch: str, client: httpx.Client) -> dict | None:
    results = _request(
        client, "GET", f"/repos/{owner}/{repo}/pulls",
        params={"head": f"{owner}:{branch}", "state": "open"},
    )
    return results[0] if results else None


def _create_pr(
    owner: str, repo: str, branch: str, base: str, *,
    title: str, body: str, draft: bool, client: httpx.Client,
) -> dict:
    try:
        return _request(
            client, "POST", f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": branch, "base": base, "draft": draft},
        )
    except GitHubError as exc:
        # The branch was just force-pushed by _push_branch above, so a PR
        # already existing for it means a prior run pushed successfully but
        # crashed or lost its ledger write before recording the PR -- find
        # that PR rather than fail a fix that has, in fact, already landed.
        if "already exists" not in str(exc):
            raise
        existing = _find_open_pr(owner, repo, branch, client)
        if existing is None:
            raise
        return existing


def _pr_body(artifact: Artifact, rule: Rule) -> str:
    lines = [
        f"Opened by `policyctl remediate` for **{artifact.title}** "
        f"(`{artifact.id}` / `{rule.id}`).",
        "",
        rule.description,
        "",
        "This diff already passed the rule's own `verify` command "
        f"(`{rule.verify}`) in a sandboxed copy of this repo before being "
        "proposed here — merging it is the only thing this tool cannot do "
        "for you.",
    ]
    if artifact.source.permalink:
        lines += ["", f"Source: {artifact.source.permalink}"]
    return "\n".join(lines)


def _request(client: httpx.Client, method: str, path: str, **kw: Any) -> Any:
    resp = client.request(method, path, **kw)
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message", resp.text)
        except Exception:
            message = resp.text
        raise GitHubError(f"{method} {path}: {resp.status_code} {message}")
    return resp.json()
