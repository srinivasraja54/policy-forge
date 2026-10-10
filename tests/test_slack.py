"""SlackSource: the real Web API reduced to the same Message shape
FixtureSource produces. No network -- every test runs against an
httpx.MockTransport, so there is no live Slack workspace in CI.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from policyforge.schema import SourceType
from policyforge.sources.slack import SlackError, SlackSource, _ref

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ok(**fields) -> dict:
    return {"ok": True, **fields}


def msg(ts: str, text: str = "x", user: str = "U1", **over) -> dict:
    return {"type": "message", "ts": ts, "text": text, "user": user, **over}


class FakeSlack:
    """A minimal, inspectable Slack backend: register canned responses per
    method, and every call made is recorded for assertions."""

    def __init__(self) -> None:
        self.responses: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []

    def on(self, method: str, *pages: dict) -> None:
        self.responses[method] = list(pages)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = dict(request.url.params)
        self.calls.append((method, params))
        pages = self.responses.get(method)
        if pages is None:
            return httpx.Response(200, json={"ok": False, "error": "not_configured"})
        page = pages.pop(0) if len(pages) > 1 else pages[0]
        return httpx.Response(200, json=page)

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="https://slack.com/api",
        )


def source(fake: FakeSlack, **kw) -> SlackSource:
    return SlackSource(channels=["C1"], token="xoxb-test", client=fake.client(), **kw)


class TestConfiguration:
    def test_no_channels_raises(self, monkeypatch):
        monkeypatch.delenv("SLACK_CHANNEL_IDS", raising=False)
        with pytest.raises(ValueError, match="no channels"):
            SlackSource(token="xoxb-test")

    def test_no_token_raises(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="no token"):
            SlackSource(channels=["C1"])

    def test_channels_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL_IDS", " C1 , C2 ,")
        s = SlackSource(token="xoxb-test")
        assert s.channels == ["C1", "C2"]


class TestRef:
    def test_matches_the_fixture_and_permalink_convention(self):
        assert _ref("C04PLATFORM", "1757600000.000100") == "C04PLATFORM/p1757600000000100"


class TestBasicFetch:
    def test_a_plain_message_is_converted(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[msg("1700000000.000100", text="hi")]))
        fake.on("users.info", ok(user={"real_name": "Jo Lee"}))
        fake.on("team.info", ok(team={"domain": "example"}))

        out = list(source(fake).fetch(EPOCH))
        assert len(out) == 1
        m = out[0]
        assert m.text == "hi"
        assert m.ref == "C1/p1700000000000100"
        assert m.source_type is SourceType.SLACK
        assert m.channel == "C1"
        assert m.author == "Jo Lee"
        assert m.permalink == "https://example.slack.com/archives/C1/p1700000000000100"
        assert m.thread_parent is None
        assert m.posted_at == datetime.fromtimestamp(1700000000.0001, tz=timezone.utc)

    def test_oldest_param_reflects_since(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[]))
        fake.on("users.info", ok(user={}))
        fake.on("team.info", ok(team={"domain": "x"}))
        since = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

        list(source(fake).fetch(since))
        method, params = fake.calls[0]
        assert method == "conversations.history"
        assert params["oldest"] == f"{since.timestamp():.6f}"
        assert params["inclusive"] == "true"

    def test_author_resolution_failure_falls_back_to_the_raw_id(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[msg("1700000000.000100")]))
        fake.on("users.info", {"ok": False, "error": "missing_scope"})
        fake.on("team.info", ok(team={"domain": "x"}))

        out = list(source(fake).fetch(EPOCH))
        assert out[0].author == "U1"

    def test_permalink_is_none_when_team_info_fails(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[msg("1700000000.000100")]))
        fake.on("users.info", ok(user={}))
        fake.on("team.info", {"ok": False, "error": "missing_scope"})

        out = list(source(fake).fetch(EPOCH))
        assert out[0].permalink is None

    def test_team_info_is_called_once_per_fetch_not_per_message(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(
            messages=[msg("1700000000.000100"), msg("1700000000.000200")]
        ))
        fake.on("users.info", ok(user={}))
        fake.on("team.info", ok(team={"domain": "x"}))

        list(source(fake).fetch(EPOCH))
        assert len([c for c in fake.calls if c[0] == "team.info"]) == 1

    def test_user_is_resolved_once_per_unique_id_not_per_message(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[
            msg("1700000000.000100", user="U1"), msg("1700000000.000200", user="U1"),
        ]))
        fake.on("users.info", ok(user={"real_name": "Jo"}))
        fake.on("team.info", ok(team={"domain": "x"}))

        list(source(fake).fetch(EPOCH))
        assert len([c for c in fake.calls if c[0] == "users.info"]) == 1


class TestThreads:
    def test_a_reply_carries_thread_parent(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[
            msg("1700000000.000100", text="root", reply_count=1),
        ]))
        fake.on("conversations.replies", ok(messages=[
            msg("1700000000.000100", text="root", thread_ts="1700000000.000100"),
            msg("1700000000.000200", text="reply", thread_ts="1700000000.000100"),
        ]))
        fake.on("users.info", ok(user={}))
        fake.on("team.info", ok(team={"domain": "x"}))

        out = list(source(fake).fetch(EPOCH))
        assert [m.text for m in out] == ["root", "reply"]
        assert out[0].thread_parent is None
        assert out[1].thread_parent == "C1/p1700000000000100"

    def test_the_threads_root_is_not_duplicated(self):
        """conversations.replies repeats the root message; it must not be
        yielded twice just because it was seen via two endpoints."""
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[
            msg("1700000000.000100", reply_count=1),
        ]))
        fake.on("conversations.replies", ok(messages=[
            msg("1700000000.000100", thread_ts="1700000000.000100"),
            msg("1700000000.000200", thread_ts="1700000000.000100"),
        ]))
        fake.on("users.info", ok(user={}))
        fake.on("team.info", ok(team={"domain": "x"}))

        out = list(source(fake).fetch(EPOCH))
        assert [m.ref for m in out] == ["C1/p1700000000000100", "C1/p1700000000000200"]

    def test_a_root_with_no_replies_does_not_call_the_replies_endpoint(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[msg("1700000000.000100")]))
        fake.on("users.info", ok(user={}))
        fake.on("team.info", ok(team={"domain": "x"}))

        list(source(fake).fetch(EPOCH))
        assert "conversations.replies" not in [c[0] for c in fake.calls]

    def test_a_failed_thread_fetch_does_not_drop_the_root(self):
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[
            msg("1700000000.000100", text="root", reply_count=1),
        ]))
        fake.on("conversations.replies", {"ok": False, "error": "thread_not_found"})
        fake.on("users.info", ok(user={}))
        fake.on("team.info", ok(team={"domain": "x"}))

        out = list(source(fake).fetch(EPOCH))
        assert [m.text for m in out] == ["root"]


class TestOrdering:
    def test_messages_are_yielded_oldest_first_across_channels(self):
        fake = FakeSlack()

        def handler(request: httpx.Request) -> httpx.Response:
            channel = dict(request.url.params).get("channel")
            ts = "1700000000.000300" if channel == "C1" else "1700000000.000100"
            fake.calls.append((request.url.path.rsplit("/", 1)[-1], dict(request.url.params)))
            if request.url.path.endswith("conversations.history"):
                return httpx.Response(200, json=ok(messages=[msg(ts)]))
            if request.url.path.endswith("users.info"):
                return httpx.Response(200, json=ok(user={}))
            return httpx.Response(200, json=ok(team={"domain": "x"}))

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://slack.com/api")
        s = SlackSource(channels=["C1", "C2"], token="t", client=client)
        out = list(s.fetch(EPOCH))
        assert [m.channel for m in out] == ["C2", "C1"], "C2's message is earlier"


class TestResilience:
    def test_one_bad_channel_does_not_block_the_others(self):
        fake = FakeSlack()

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            fake.calls.append((request.url.path.rsplit("/", 1)[-1], params))
            if request.url.path.endswith("conversations.history"):
                if params.get("channel") == "BAD":
                    return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
                return httpx.Response(200, json=ok(messages=[msg("1700000000.000100")]))
            if request.url.path.endswith("users.info"):
                return httpx.Response(200, json=ok(user={}))
            return httpx.Response(200, json=ok(team={"domain": "x"}))

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://slack.com/api")
        s = SlackSource(channels=["BAD", "GOOD"], token="t", client=client)
        out = list(s.fetch(EPOCH))
        assert len(out) == 1
        assert out[0].channel == "GOOD"

    def test_rate_limit_is_retried(self):
        state = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("conversations.history"):
                state["n"] += 1
                if state["n"] == 1:
                    return httpx.Response(429, headers={"Retry-After": "0"})
                return httpx.Response(200, json=ok(messages=[msg("1700000000.000100")]))
            if request.url.path.endswith("users.info"):
                return httpx.Response(200, json=ok(user={}))
            return httpx.Response(200, json=ok(team={"domain": "x"}))

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://slack.com/api")
        s = SlackSource(channels=["C1"], token="t", client=client)
        out = list(s.fetch(EPOCH))
        assert len(out) == 1
        assert state["n"] == 2

    def test_exhausted_retries_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "0"})

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://slack.com/api")
        s = SlackSource(channels=["C1"], token="t", client=client)
        with pytest.raises(SlackError, match="rate limited"):
            list(s._history("C1", "0"))

    def test_http_error_propagates(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://slack.com/api")
        s = SlackSource(channels=["C1"], token="t", client=client)
        with pytest.raises(httpx.HTTPStatusError):
            list(s._history("C1", "0"))


class TestPagination:
    def test_follows_next_cursor(self):
        fake = FakeSlack()
        fake.responses["conversations.history"] = [
            {"ok": True, "messages": [msg("1700000000.000100")],
             "response_metadata": {"next_cursor": "abc"}},
            {"ok": True, "messages": [msg("1700000000.000200")],
             "response_metadata": {"next_cursor": ""}},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("conversations.history"):
                pages = fake.responses["conversations.history"]
                return httpx.Response(200, json=pages.pop(0) if len(pages) > 1 else pages[0])
            if request.url.path.endswith("users.info"):
                return httpx.Response(200, json=ok(user={}))
            return httpx.Response(200, json=ok(team={"domain": "x"}))

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://slack.com/api")
        s = SlackSource(channels=["C1"], token="t", client=client)
        out = list(s.fetch(EPOCH))
        assert [m.ref for m in out] == ["C1/p1700000000000100", "C1/p1700000000000200"]

    def test_a_message_with_no_user_has_no_author(self):
        """Bot/system messages carry no `user` field. Must not be an error."""
        fake = FakeSlack()
        fake.on("conversations.history", ok(messages=[msg("1700000000.000100", user=None)]))
        fake.on("users.info", ok(user={}))
        fake.on("team.info", ok(team={"domain": "x"}))

        out = list(source(fake).fetch(EPOCH))
        assert out[0].author is None
        assert "users.info" not in [c[0] for c in fake.calls]
