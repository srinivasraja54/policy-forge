"""Loading and validating the artifact registry.

The registry is a directory of JSON files under git. It is the system of record:
the Slack message is ephemeral, the artifact is institutional memory. Loading is
strict -- a malformed artifact is an error, never a silent skip, because a
silently-dropped policy is indistinguishable from a policy that passes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .schema import Artifact, Status


class RegistryError(Exception):
    """Raised when the registry cannot be loaded or is internally inconsistent."""


class Repo(BaseModel):
    """One entry in the repo catalog."""

    model_config = ConfigDict(extra="forbid")

    name: str
    path: str | None = Field(default=None, description="Local checkout, for offline runs")
    team: str
    owners: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


@dataclass
class Catalog:
    repos: list[Repo] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Catalog:
        if not path.exists():
            raise RegistryError(f"catalog not found: {path}")
        raw = yaml.safe_load(path.read_text()) or {}
        try:
            repos = [Repo(**r) for r in raw.get("repos", [])]
        except ValidationError as exc:
            raise RegistryError(f"invalid catalog {path}:\n{exc}") from exc
        names = [r.name for r in repos]
        if len(set(names)) != len(names):
            raise RegistryError("duplicate repo names in catalog")
        return cls(repos=repos)

    def by_name(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    def teams(self) -> set[str]:
        return {r.team for r in self.repos}


@dataclass
class Registry:
    artifacts: list[Artifact] = field(default_factory=list)
    catalog: Catalog = field(default_factory=Catalog)
    root: Path | None = None
    # id -> the file it was loaded from. An amendment rewrites an artifact in
    # place, and the filename is not guaranteed to be derivable from the id, so
    # the only safe answer is the path we actually read.
    paths: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> Registry:
        root = Path(root)
        if not root.exists():
            raise RegistryError(f"registry root not found: {root}")

        artifacts: list[Artifact] = []
        paths: dict[str, Path] = {}
        errors: list[str] = []
        for f in sorted(root.rglob("*.json")):
            try:
                data = json.loads(f.read_text())
            except json.JSONDecodeError as exc:
                errors.append(f"{f}: not valid JSON -- {exc}")
                continue
            try:
                artifact = Artifact(**data)
            except ValidationError as exc:
                errors.append(f"{f}:\n{_indent(str(exc))}")
                continue
            artifacts.append(artifact)
            paths[artifact.id] = f
        if errors:
            raise RegistryError(
                f"{len(errors)} invalid artifact(s):\n\n" + "\n\n".join(errors)
            )

        ids = [a.id for a in artifacts]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise RegistryError(f"duplicate artifact ids: {', '.join(dupes)}")

        known = set(ids)
        for a in artifacts:
            if a.superseded_by and a.superseded_by not in known:
                raise RegistryError(
                    f"{a.id}: superseded_by points at unknown artifact "
                    f"'{a.superseded_by}'"
                )

        catalog_path = root / "catalog.yaml"
        catalog = Catalog.load(catalog_path) if catalog_path.exists() else Catalog()
        return cls(artifacts=artifacts, catalog=catalog, root=root, paths=paths)

    def enforceable(self, now=None) -> list[Artifact]:
        return [a for a in self.artifacts if a.is_enforceable(now)]

    def active(self) -> list[Artifact]:
        return [a for a in self.artifacts if a.status is Status.ACTIVE]

    def by_id(self, artifact_id: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.id == artifact_id), None)

    def source_refs(self) -> dict[str, str]:
        """Every message ref that has already produced output, mapped to the
        artifact it produced or amended.

        This is the compiler's durable dedupe and amendment-target resolution,
        with no side database: a message already represented here needs no
        ledger to be remembered, because the registry already is the record --
        it is git, checked out fresh on every run, including in CI where
        nothing else persists between invocations. An artifact's own
        `source.ref`, and every `Amendment.source.ref` recorded on it, both
        count; either means this exact message already has a durable effect
        that reprocessing it would duplicate.
        """
        refs: dict[str, str] = {}
        for a in self.artifacts:
            refs.setdefault(a.source.ref, a.id)
            for amendment in a.amendments:
                refs.setdefault(amendment.source.ref, a.id)
        return refs


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
