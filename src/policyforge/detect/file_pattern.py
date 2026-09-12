"""Regex-over-globs detection, with two interchangeable backends.

Shelling out to ripgrep rather than walking the tree in Python is deliberate:
on a large monorepo it is one to two orders of magnitude faster, and its JSON
output already carries line numbers and matched text. Where rg is absent we walk
the tree in Python instead, so the engine runs anywhere -- including CI images
and developer laptops that do not ship ripgrep.

The two backends are required to agree. That is enforced upstream rather than
here: `FilePatternDetect` validates every regex by compiling it with Python's
`re` at authoring time, so a pattern using PCRE2-only syntax (atomic groups,
recursion, variable-length lookbehind) is rejected when the artifact is written,
never at enforcement time on some subset of machines. The registry therefore
only ever holds patterns both engines understand.

Resolution order for the binary: $POLICYFORGE_RG, then PATH. The override exists
because `rg` is sometimes a shell function or alias rather than an executable, in
which case shutil.which finds nothing and we would silently take the slow path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..schema import FilePatternDetect
from .base import Finding, Outcome, RepoTarget, RuleResult

_MAX_EXCERPT = 300


def _resolve_rg() -> str | None:
    override = os.environ.get("POLICYFORGE_RG")
    if override:
        return override if (Path(override).is_file() and os.access(override, os.X_OK)) else None
    return shutil.which("rg")


RG = _resolve_rg()


def backend() -> str:
    """Which backend a search would use right now. Surfaced so runs are auditable
    -- a silent switch between backends should never be invisible in a log."""
    return "ripgrep" if RG else "python"


def run(
    detect: FilePatternDetect,
    target: RepoTarget,
    *,
    artifact_id: str,
    rule_id: str,
) -> RuleResult:
    def result(outcome: Outcome, findings=None, error=None) -> RuleResult:
        return RuleResult(
            artifact_id=artifact_id,
            rule_id=rule_id,
            repo=target.name,
            outcome=outcome,
            findings=findings or [],
            error=error,
        )

    if not target.path.exists():
        return result(Outcome.ERROR, error=f"path does not exist: {target.path}")

    try:
        matches = _search(detect, target.path)
    except Exception as exc:  # tool failure must never be read as "clear"
        return result(Outcome.ERROR, error=f"{type(exc).__name__}: {exc}")

    if detect.absent:
        # Violation is the absence of a match in files the globs select.
        matched_files = {m.file for m in matches}
        missing = [
            Finding(file=str(p), line=0, excerpt="", detail="required pattern not found")
            for p in sorted(_iter_glob_files(detect.globs, target.path))
            if str(p) not in matched_files
        ]
        return result(Outcome.VIOLATING if missing else Outcome.CLEAR, missing)

    return result(Outcome.VIOLATING if matches else Outcome.CLEAR, matches)


def _search(detect: FilePatternDetect, root: Path) -> list[Finding]:
    if RG:
        return _search_ripgrep(detect, root)
    return _search_python(detect, root)


def _search_ripgrep(detect: FilePatternDetect, root: Path) -> list[Finding]:
    cmd = [RG, "--json", "--pcre2", "--no-config", "--multiline", detect.regex]
    for g in detect.globs:
        cmd += ["--glob", g]
    cmd.append(".")

    proc = subprocess.run(
        cmd, cwd=root, capture_output=True, text=True, timeout=120, check=False
    )
    # rg exit codes: 0 = matches, 1 = no matches, 2 = error.
    if proc.returncode == 1:
        return []
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ripgrep failed ({proc.returncode}): {proc.stderr.strip()}")

    findings: list[Finding] = []
    for line in proc.stdout.splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "match":
            continue
        d = evt["data"]
        findings.append(
            Finding(
                file=_rel(d["path"]),
                line=d["line_number"] or 0,
                excerpt=_text(d["lines"])[:_MAX_EXCERPT].rstrip("\n"),
            )
        )
    return findings


def _search_python(detect: FilePatternDetect, root: Path) -> list[Finding]:
    pattern = re.compile(detect.regex, re.MULTILINE)
    findings: list[Finding] = []
    for path in _iter_glob_files(detect.globs, root):
        try:
            text = (root / path).read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                findings.append(
                    Finding(file=str(path), line=n, excerpt=line[:_MAX_EXCERPT])
                )
    return findings


def _iter_glob_files(globs: list[str], root: Path):
    seen: set[Path] = set()
    for g in globs:
        # Treat "**/x" and "x" alike so a glob written for repo root still matches.
        for pattern in {g, f"**/{g}"} if not g.startswith("**/") else {g}:
            for p in root.glob(pattern):
                if p.is_file():
                    rel = p.relative_to(root)
                    if rel not in seen and ".git" not in rel.parts:
                        seen.add(rel)
                        yield rel


def _rel(path_obj) -> str:
    raw = _text(path_obj)
    return raw[2:] if raw.startswith("./") else raw


def _text(obj) -> str:
    """rg emits either {"text": ...} or {"bytes": <base64>} for non-UTF8 data."""
    if isinstance(obj, dict):
        if "text" in obj:
            return obj["text"]
        if "bytes" in obj:
            import base64

            return base64.b64decode(obj["bytes"]).decode("utf-8", errors="replace")
    return str(obj)
