"""Source adapters: where raw org signals come from.

Deliberately pluggable. Slack announcements, postmortems and maintenance notices
are different *inputs* to one compiler, not three separate systems -- so the
interface is narrow and every adapter reduces to the same `Message` shape.

`ref` is the contract that makes the pipeline cheap: it is the stable dedupe key
recorded on whatever artifact a thread produces (see `Registry.source_refs()`),
so a thread already compiled or amended is skipped by a dict lookup rather than
by asking a model to recognise it again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol, runtime_checkable

from ..schema import SourceType


@dataclass(frozen=True)
class Message:
    """One raw signal, before any interpretation."""

    ref: str
    text: str
    posted_at: datetime
    source_type: SourceType
    author: str | None = None
    channel: str | None = None
    permalink: str | None = None
    # Set when this message replies to another. Replies are not skipped -- they
    # are routed as potential amendments to the artifact the parent produced.
    thread_parent: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def is_reply(self) -> bool:
        return self.thread_parent is not None

    def truncated(self, limit: int = 4000) -> str:
        """Bound what reaches the model. Announcements are short; a pasted log
        dump is not, and paying to reason over one is waste."""
        if len(self.text) <= limit:
            return self.text
        return self.text[:limit] + f"\n[... {len(self.text) - limit} more chars]"


@runtime_checkable
class SourceAdapter(Protocol):
    """Implemented by FixtureSource now, SlackSource in Phase 3."""

    name: str

    def fetch(self, since: datetime) -> Iterable[Message]:
        """Messages posted at or after `since`, oldest first."""
        ...
