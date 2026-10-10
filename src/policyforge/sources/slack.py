"""Slack ingestion: the SourceAdapter Phase 1/2 already built the pipeline around.

Reduces Slack's two-endpoint, paginated, newest-first API to the same flat,
oldest-first Message stream FixtureSource already produces -- nothing
downstream (triage, compile, amendment resolution) knows or cares which
adapter it is reading from.

Two endpoints, because Slack splits a channel's history from its threads:
`conversations.history` returns only the root of each thread, never the
replies; `conversations.replies` returns a thread's full contents, root
included. A root with `reply_count > 0` needs a second call to see what was
actually said in response to it -- which is exactly the amendment material
this project cares about, so it is not optional.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from ..schema import SourceType
from .base import Message

log = logging.getLogger(__name__)

_API = "https://slack.com/api"
_PAGE_SIZE = 200
_MAX_RETRIES = 3


class SlackError(Exception):
    """A Slack API call failed outright: bad token, missing scope, bad channel."""


class SlackSource:
    """Implements SourceAdapter against the real Slack Web API."""

    name = "slack"

    def __init__(
        self,
        channels: list[str] | None = None,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.channels = channels or _channels_from_env()
        if not self.channels:
            raise ValueError(
                "no channels configured -- pass channels=[...] or set "
                "SLACK_CHANNEL_IDS (comma-separated channel ids)"
            )
        token = token or os.environ.get("SLACK_BOT_TOKEN")
        if not token and client is None:
            raise ValueError("no token -- pass token=... or set SLACK_BOT_TOKEN")
        self._client = client or httpx.Client(
            base_url=_API, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
        )
        self._user_cache: dict[str, str] = {}
        self._domain: str | None = None
        self._domain_fetched = False

    def fetch(self, since: datetime) -> Iterator[Message]:
        """Messages posted at or after `since`, oldest first, across every
        configured channel -- including thread replies within the window.

        Collected into a list and sorted before yielding, rather than streamed
        as each channel is read: a reply's ts is always after its thread
        parent's, but replies across channels and threads are not fetched in
        that order, and amendment resolution within one run depends on a
        parent being handled before the message that targets it.
        """
        oldest = f"{since.astimezone(timezone.utc).timestamp():.6f}"
        seen: set[str] = set()
        messages: list[Message] = []

        for channel in self.channels:
            try:
                roots = list(self._history(channel, oldest))
            except SlackError as exc:
                # One bad channel (wrong id, bot not invited) must not silence
                # every other configured channel.
                log.warning("slack: channel %s failed, skipping: %s", channel, exc)
                continue

            for raw in roots:
                self._collect(channel, raw, oldest, messages, seen)

        messages.sort(key=lambda m: m.posted_at)
        yield from messages

    # ---- collecting one root and its thread, if any -----------------------

    def _collect(
        self, channel: str, raw: dict, oldest: str,
        out: list[Message], seen: set[str],
    ) -> None:
        ref = _ref(channel, raw["ts"])
        if ref not in seen:
            seen.add(ref)
            out.append(self._to_message(channel, raw))

        if float(raw.get("reply_count") or 0) <= 0:
            return
        try:
            # _replies is a generator: the call itself never raises, only
            # iterating it does, so it has to be materialised inside the try.
            replies = list(self._replies(channel, raw["ts"], oldest))
        except SlackError as exc:
            log.warning("slack: thread %s in %s failed, skipping replies: %s",
                        raw["ts"], channel, exc)
            return
        for reply in replies:
            rref = _ref(channel, reply["ts"])
            if rref in seen:
                # conversations.replies repeats the thread's root message
                continue
            seen.add(rref)
            out.append(self._to_message(channel, reply))

    def _to_message(self, channel: str, raw: dict) -> Message:
        ts = raw["ts"]
        thread_ts = raw.get("thread_ts")
        return Message(
            ref=_ref(channel, ts),
            text=raw.get("text", ""),
            posted_at=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            source_type=SourceType.SLACK,
            author=self._author(raw.get("user")),
            channel=channel,
            permalink=self._permalink(channel, ts),
            thread_parent=_ref(channel, thread_ts) if thread_ts and thread_ts != ts else None,
            meta={"slack_ts": ts},
        )

    # ---- Slack API ----------------------------------------------------

    def _history(self, channel: str, oldest: str) -> Iterator[dict]:
        yield from self._paginate("conversations.history", {
            "channel": channel, "oldest": oldest, "inclusive": "true",
            "limit": _PAGE_SIZE,
        })

    def _replies(self, channel: str, ts: str, oldest: str) -> Iterator[dict]:
        yield from self._paginate("conversations.replies", {
            "channel": channel, "ts": ts, "oldest": oldest, "inclusive": "true",
            "limit": _PAGE_SIZE,
        })

    def _paginate(self, method: str, params: dict) -> Iterator[dict]:
        cursor: str | None = None
        while True:
            page = dict(params)
            if cursor:
                page["cursor"] = cursor
            data = self._call(method, page)
            yield from data.get("messages", [])
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return

    def _call(self, method: str, params: dict) -> dict[str, Any]:
        for attempt in range(_MAX_RETRIES):
            resp = self._client.get(f"/{method}", params=params)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", "1")))
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise SlackError(f"{method}: {data.get('error', 'unknown error')}")
            return data
        raise SlackError(f"{method}: rate limited after {_MAX_RETRIES} attempts")

    # ---- provenance, best-effort ----------------------------------------

    def _author(self, user_id: str | None) -> str | None:
        """A display name for the prompt, not a correctness requirement: any
        failure here falls back to the raw id rather than failing the fetch."""
        if not user_id:
            return None
        if user_id not in self._user_cache:
            try:
                data = self._call("users.info", {"user": user_id})
                user = data["user"]
                self._user_cache[user_id] = user.get("real_name") or user.get("name") or user_id
            except Exception:
                self._user_cache[user_id] = user_id
        return self._user_cache[user_id]

    def _permalink(self, channel: str, ts: str) -> str | None:
        domain = self._team_domain()
        if not domain:
            return None
        # The same shape Slack's own "copy link" produces; one team.info call
        # per fetch() builds it locally rather than one chat.getPermalink call
        # per message.
        return f"https://{domain}.slack.com/archives/{channel}/p{ts.replace('.', '')}"

    def _team_domain(self) -> str | None:
        if not self._domain_fetched:
            self._domain_fetched = True
            try:
                self._domain = self._call("team.info", {})["team"]["domain"]
            except Exception:
                self._domain = None
        return self._domain


def _ref(channel: str, ts: str) -> str:
    """The stable dedupe key, matching the format Slack's own permalinks use
    (and the format already baked into every fixture-compiled artifact):
    `<channel>/p<ts with the decimal point removed>`.
    """
    return f"{channel}/p{ts.replace('.', '')}"


def _channels_from_env() -> list[str]:
    raw = os.environ.get("SLACK_CHANNEL_IDS", "")
    return [c.strip() for c in raw.split(",") if c.strip()]
