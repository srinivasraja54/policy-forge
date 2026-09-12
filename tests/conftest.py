from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def registry_dir(repo_root: Path) -> Path:
    return repo_root / "registry"


@pytest.fixture
def fixtures_dir(repo_root: Path) -> Path:
    return repo_root / "fixtures" / "repos"


@pytest.fixture
def source_stub() -> dict:
    return {
        "type": "slack",
        "ref": "C0TEST/p1",
        "captured_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }


@pytest.fixture
def simple_rule() -> dict:
    return {
        "id": "no-latest",
        "description": "no :latest tags",
        "detect": {
            "type": "file_pattern",
            "globs": ["**/Dockerfile"],
            "regex": r"^FROM\s+\S+:latest",
        },
    }
