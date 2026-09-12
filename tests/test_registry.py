"""Registry loading.

Loading is deliberately strict. A policy that fails to parse and is silently
skipped looks exactly like a policy that everyone passes, so every failure mode
here must raise rather than degrade.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from policyforge.registry import Catalog, Registry, RegistryError

VALID_SOURCE = {"type": "slack", "ref": "C0/p1", "captured_at": "2026-09-01T00:00:00Z"}


def write_artifact(root: Path, name: str, **over) -> Path:
    body = {
        "schema_version": 1, "kind": "policy", "id": name, "title": "t",
        "status": "active", "source": VALID_SOURCE,
        "rules": [{"id": "r1", "description": "d",
                   "detect": {"type": "file_pattern", "globs": ["**/x"],
                              "regex": "y"}}],
    }
    body.update(over)
    path = root / f"{name}.json"
    path.write_text(json.dumps(body))
    return path


class TestLoading:
    def test_loads_real_registry(self, registry_dir):
        reg = Registry.load(registry_dir)
        assert len(reg.artifacts) == 2
        assert len(reg.catalog.repos) == 3

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(RegistryError, match="not found"):
            Registry.load(tmp_path / "nope")

    def test_empty_registry_is_valid(self, tmp_path):
        reg = Registry.load(tmp_path)
        assert reg.artifacts == []

    def test_recurses_subdirectories(self, tmp_path):
        (tmp_path / "policies").mkdir()
        (tmp_path / "events").mkdir()
        write_artifact(tmp_path / "policies", "pol-one")
        write_artifact(tmp_path / "events", "evt-one")
        assert len(Registry.load(tmp_path).artifacts) == 2


class TestFailureModes:
    def test_malformed_json_raises(self, tmp_path):
        (tmp_path / "bad.json").write_text("{not json")
        with pytest.raises(RegistryError, match="not valid JSON"):
            Registry.load(tmp_path)

    def test_schema_violation_raises(self, tmp_path):
        write_artifact(tmp_path, "bad", kind="event")  # event with no window
        with pytest.raises(RegistryError, match="invalid artifact"):
            Registry.load(tmp_path)

    def test_duplicate_ids_raise(self, tmp_path):
        write_artifact(tmp_path, "dup")
        (tmp_path / "copy.json").write_text((tmp_path / "dup.json").read_text())
        with pytest.raises(RegistryError, match="duplicate artifact ids"):
            Registry.load(tmp_path)

    def test_dangling_superseded_by_raises(self, tmp_path):
        write_artifact(tmp_path, "orphan", status="superseded", superseded_by="ghost")
        with pytest.raises(RegistryError, match="unknown artifact"):
            Registry.load(tmp_path)

    def test_resolvable_superseded_by_loads(self, tmp_path):
        write_artifact(tmp_path, "old", status="superseded", superseded_by="new")
        write_artifact(tmp_path, "new")
        assert len(Registry.load(tmp_path).artifacts) == 2

    def test_all_errors_reported_together(self, tmp_path):
        """One pass should surface every problem, not just the first."""
        (tmp_path / "a.json").write_text("{bad")
        (tmp_path / "b.json").write_text("{also bad")
        with pytest.raises(RegistryError, match="2 invalid artifact"):
            Registry.load(tmp_path)


class TestSelectors:
    def test_enforceable_excludes_non_active(self, tmp_path):
        write_artifact(tmp_path, "live", status="active")
        write_artifact(tmp_path, "draft", status="proposed")
        write_artifact(tmp_path, "dead", status="retired")
        reg = Registry.load(tmp_path)
        assert [a.id for a in reg.enforceable()] == ["live"]

    def test_active_includes_all_active(self, tmp_path):
        write_artifact(tmp_path, "act-one", status="active")
        write_artifact(tmp_path, "prop-one", status="proposed")
        assert [a.id for a in Registry.load(tmp_path).active()] == ["act-one"]

    def test_by_id_returns_none_when_absent(self, registry_dir):
        assert Registry.load(registry_dir).by_id("nope") is None


class TestCatalog:
    def test_missing_catalog_yields_empty(self, tmp_path):
        assert Registry.load(tmp_path).catalog.repos == []

    def test_duplicate_repo_names_raise(self, tmp_path):
        (tmp_path / "catalog.yaml").write_text(yaml.safe_dump({
            "repos": [{"name": "x", "team": "a"}, {"name": "x", "team": "b"}]
        }))
        with pytest.raises(RegistryError, match="duplicate repo names"):
            Catalog.load(tmp_path / "catalog.yaml")

    def test_unknown_catalog_field_raises(self, tmp_path):
        """Typos in the catalog must not silently drop ownership routing."""
        (tmp_path / "catalog.yaml").write_text(yaml.safe_dump({
            "repos": [{"name": "x", "team": "a", "ownerz": ["@me"]}]
        }))
        with pytest.raises(RegistryError, match="invalid catalog"):
            Catalog.load(tmp_path / "catalog.yaml")

    def test_lookup_helpers(self, registry_dir):
        cat = Registry.load(registry_dir).catalog
        assert cat.by_name("web-ui").team == "frontend"
        assert cat.by_name("nope") is None
        assert cat.teams() == {"payments", "data", "frontend"}
