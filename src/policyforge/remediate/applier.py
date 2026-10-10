"""Deterministic remediation: Autofix + Finding -> a concrete, reviewable change.

Mirrors detection's own contract. `policyctl check` never guesses at what a
regex means; it just runs it. An autofix does the same: it is a mechanical
consequence of the rule's own `detect.regex`, not a second, independent
opinion about what the violation is. Where that is not possible -- an absence
rule has no match to anchor an insertion to, a `manual` autofix says a human
must look, a matched reference is itself a build variable -- this refuses
rather than improvising.

No model calls. Computing a candidate fix is one of the two steps the
registry's `verify` oracle is built to make cheap to get wrong; the other is
`verify.py`, which actually runs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..detect.base import Finding, RepoTarget
from ..schema import (
    Autofix,
    FilePatternDetect,
    ManualFix,
    PrefixRewrite,
    RegexReplace,
    Rule,
    ShiftSchedule,
)

# Docker/shell variable expansion: `$BASE_IMAGE`, `${BASE_IMAGE}`,
# `${BASE_IMAGE:-default}`. Prepending a registry prefix in front of one of
# these does not fix the reference -- it silently breaks the interpolation,
# producing a Dockerfile that builds nothing. This is exactly the case the
# project's own design says must surface for a human, not get mis-fixed.
#
# No `^` here: matched with `.match(text, pos)` below, which already anchors
# the attempt to start exactly at `pos` -- `^` without MULTILINE anchors to
# true string index 0 regardless of `pos`, so adding it would silently turn
# this into a check that only ever fires on the first line of a file.
_VARIABLE_REF = re.compile(r"\$")


class RemediationError(Exception):
    """An autofix that cannot be mechanically applied at all -- the whole rule
    has no path forward (no autofix, `manual`, wrong detector kind, an absence
    rule). Never raised for a single unfixable finding among otherwise-fixable
    ones; see `SkippedFix` for that."""


@dataclass(frozen=True)
class SkippedFix:
    """One finding `plan_fix` deliberately did not touch, and why."""

    file: str
    reason: str


@dataclass(frozen=True)
class FileChange:
    """One file's proposed before/after. Not yet written anywhere -- a caller
    decides whether to verify it, diff it, or commit it to disk."""

    path: str
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def unified_diff(self) -> str:
        import difflib

        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"a/{self.path}",
                tofile=f"b/{self.path}",
            )
        )


def plan_fix(
    rule: Rule, target: RepoTarget, findings: list[Finding]
) -> tuple[list[FileChange], list[SkippedFix]]:
    """Compute the file changes that would resolve `findings` for `rule`.

    Returns `(changes, skipped)`: one `FileChange` per file that was safe to
    fix mechanically, and one `SkippedFix` per file that was not -- a variable
    reference, say. The two are kept apart rather than one aborting the other,
    so a mixed batch still fixes what it safely can and names what it did not,
    instead of either silently fixing something wrong or refusing everything
    because one file was tricky.

    Raises `RemediationError` up front, before touching any file, when the
    rule itself has no mechanical path at all -- that is a property of the
    rule, not of any one finding.
    """
    if rule.autofix is None:
        raise RemediationError(f"rule {rule.id!r} has no autofix")
    if isinstance(rule.autofix, ManualFix):
        raise RemediationError(
            f"rule {rule.id!r} is manual-only: {rule.autofix.instructions}"
        )
    if not isinstance(rule.detect, FilePatternDetect):
        raise RemediationError(
            f"rule {rule.id!r}: autofix only applies file_pattern detectors "
            "mechanically; schedule_intersects fixes are not yet automated"
        )
    if rule.detect.absent:
        raise RemediationError(
            f"rule {rule.id!r}: cannot autofix an absence rule -- there is no "
            "match to anchor an insertion to"
        )

    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)

    changes: list[FileChange] = []
    skipped: list[SkippedFix] = []
    for file in sorted(by_file):
        path = target.path / file
        try:
            before = path.read_text()
        except OSError as exc:
            skipped.append(SkippedFix(file=file, reason=f"cannot read -- {exc}"))
            continue
        try:
            after = _apply(rule.detect.regex, rule.autofix, before, file=file)
        except RemediationError as exc:
            skipped.append(SkippedFix(file=file, reason=str(exc)))
            continue
        changes.append(FileChange(path=file, before=before, after=after))
    return changes, skipped


def _apply(detect_regex: str, autofix: Autofix, text: str, *, file: str) -> str:
    if isinstance(autofix, PrefixRewrite):
        after = _apply_prefix_rewrite(detect_regex, autofix.to, text)
    elif isinstance(autofix, RegexReplace):
        # Self-contained: its own pattern, not necessarily the detector's. This
        # is how a fix can target a narrower span than what flagged it -- e.g.
        # rewriting only the tag, not the whole FROM line.
        after = re.sub(autofix.pattern, autofix.replacement, text, flags=re.MULTILINE)
    elif isinstance(autofix, ShiftSchedule):
        raise RemediationError(
            "shift_schedule applies to schedule_intersects rules, not "
            "file_pattern -- it is not reachable here"
        )
    else:
        raise RemediationError(f"unsupported autofix type: {type(autofix).__name__}")

    # The oracle that costs nothing: whatever the autofix did, re-running the
    # rule's own detector against the result must no longer match. If it does,
    # the fix and the violation disagree about where the problem is, and that
    # is a bug in the rule, not something to apply and hope about.
    if re.compile(detect_regex, re.MULTILINE).search(after):
        raise RemediationError(
            f"{file}: fix did not clear the detector's own pattern -- the "
            "autofix and detect.regex disagree about what the violation is"
        )
    return after


def _apply_prefix_rewrite(detect_regex: str, to: str, text: str) -> str:
    """Prepend `to` at the point `detect_regex` stopped matching.

    The convention `docs/artifacts.md` documents: a detector like
    `^FROM\\s+(?!mirror/)` matches zero-width at exactly the spot a mirror
    prefix belongs, which is what makes this mechanical rather than a guess --
    the detect regex already finding that spot is the thing that makes autofix
    safe to automate at all. Processed match-by-match, not as one blind
    substitution, so one variable reference in a file does not silently decide
    the fate of every other match in it.
    """
    pattern = re.compile(detect_regex, re.MULTILINE)
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        out.append(text[pos : m.end()])
        if _VARIABLE_REF.match(text, m.end()):
            raise RemediationError(
                f"match at offset {m.start()} is a build variable, not a "
                "literal reference -- prepending a prefix would silently break "
                "it rather than fix it"
            )
        out.append(to)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)
