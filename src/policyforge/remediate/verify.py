"""Running a rule's `verify` command against a candidate fix.

This is the oracle the whole project is built around: a fix can be proposed
cheaply and confidently because verifying it is free and certain, in the same
sense `policyctl check` can run on every push because detection is free and
certain. A fix that fails `verify` is not applied, and it is not reported as
"probably fine" for a human to bless -- it simply did not pass.

Trust model: `verify` is a field on an artifact, and artifacts only run here
once they are `status: active` -- merged through the registry pull request
that is this project's one review gate. The command is exactly as trusted as
any other reviewed string in that repository; this module does not add a
second sandbox beyond the one the command's own effects call for (see below).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .applier import FileChange

DEFAULT_TIMEOUT = 300


@dataclass(frozen=True)
class VerifyResult:
    passed: bool
    command: str
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


def verify_in_sandbox(
    repo_path: Path,
    changes: list[FileChange],
    command: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> VerifyResult:
    """Copy the repo, apply `changes` to the copy, run `command` there.

    Always a copy, never the real checkout -- a verify command that happens to
    write (a build cache, a generated lockfile) must not leave side effects on
    a repo a human is about to review a diff against, and a fix that fails
    verify must leave no trace at all.
    """
    with tempfile.TemporaryDirectory(prefix="policyforge-verify-") as tmp:
        sandbox = Path(tmp) / "repo"
        shutil.copytree(repo_path, sandbox, ignore=shutil.ignore_patterns(".git"))
        for change in changes:
            (sandbox / change.path).write_text(change.after)

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=sandbox,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return VerifyResult(
                passed=False,
                command=command,
                returncode=None,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                timed_out=True,
            )

        return VerifyResult(
            passed=proc.returncode == 0,
            command=command,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


def _decode(output: str | bytes | None) -> str:
    if output is None:
        return ""
    return output if isinstance(output, str) else output.decode(errors="replace")
