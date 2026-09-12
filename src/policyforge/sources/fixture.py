"""Fixture source: reads messages from JSON on disk.

Exists so the entire pipeline -- triage, compile, artifact emission -- can be
developed, tested and demonstrated with no Slack workspace and no credentials.
The Slack adapter in Phase 3 implements the same protocol, so nothing downstream
changes when it is swapped in.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..schema import SourceType
from .base import Message


class FixtureSource:
    name = "fixture"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def fetch(self, since: datetime) -> Iterator[Message]:
        for f in sorted(self.path.glob("*.json")):
            payload = json.loads(f.read_text())
            entries = payload if isinstance(payload, list) else [payload]
            for entry in entries:
                msg = self._to_message(entry)
                if msg.posted_at >= since:
                    yield msg

    @staticmethod
    def _to_message(entry: dict) -> Message:
        return Message(
            ref=entry["ref"],
            text=entry["text"],
            posted_at=datetime.fromisoformat(entry["posted_at"]),
            source_type=SourceType(entry.get("source_type", "slack")),
            author=entry.get("author"),
            channel=entry.get("channel"),
            permalink=entry.get("permalink"),
            thread_parent=entry.get("thread_parent"),
            meta=entry.get("meta", {}),
        )
