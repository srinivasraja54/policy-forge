"""Request shaping for the real SDK client.

The network call cannot run in CI, but the shape of the request can and should be
asserted. This is where API drift hides: a `budget_tokens` carried over from an
older model, `output_format` where `output_config` is now required, a missing
cache_control that silently drops the hit rate to zero. A fake SDK module
captures the kwargs so all of that is checked without a key or a network.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import pytest

from policyforge.compiler.engine import COMPILE_MODEL, TRIAGE_MODEL, AnthropicClient
from policyforge.compiler.models import ProposedArtifact, TriageVerdict


@dataclass
class _Recorder:
    calls: list[dict] = field(default_factory=list)
    stop_reason: str | None = None

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        schema = kwargs["output_format"]
        parsed = (
            TriageVerdict(ref="r", actionable=False, kind="none", reason="x",
                          confidence=0.5)
            if schema is TriageVerdict
            else ProposedArtifact(
                kind="policy", id="abc-2027", title="t", severity="warn",
                confidence=0.8, reasoning="r",
                rules=[{"id": "rr", "description": "d",
                        "detect": {"type": "file_pattern", "globs": ["**/x"],
                                   "regex": "y"}}],
            )
        )
        usage = types.SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=3, cache_creation_input_tokens=0,
        )
        return types.SimpleNamespace(
            parsed_output=parsed, usage=usage,
            stop_reason=self.stop_reason,
            stop_details=types.SimpleNamespace(category="cyber"),
        )


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a stand-in `anthropic` module before AnthropicClient imports it."""
    rec = _Recorder()
    module = types.ModuleType("anthropic")

    class _Anthropic:
        def __init__(self, *a, **k):
            self.messages = rec
            self.beta = types.SimpleNamespace(messages=rec)

    module.Anthropic = _Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return rec


def call(client, model):
    schema = TriageVerdict if model == TRIAGE_MODEL else ProposedArtifact
    return client.parse(model=model, system="SYS", user="USR", schema=schema)


class TestCommonShape:
    def test_system_prompt_is_marked_for_caching(self, fake_sdk):
        call(AnthropicClient(), TRIAGE_MODEL)
        system = fake_sdk.calls[0]["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[0]["text"] == "SYS"

    def test_user_turn_carries_the_volatile_content(self, fake_sdk):
        """Volatile content must sit after the cached prefix, never inside it."""
        call(AnthropicClient(), TRIAGE_MODEL)
        kwargs = fake_sdk.calls[0]
        assert kwargs["messages"] == [{"role": "user", "content": "USR"}]
        assert "USR" not in kwargs["system"][0]["text"]

    def test_structured_output_is_requested(self, fake_sdk):
        call(AnthropicClient(), TRIAGE_MODEL)
        assert fake_sdk.calls[0]["output_format"] is TriageVerdict

    def test_max_tokens_is_not_lowballed(self, fake_sdk):
        call(AnthropicClient(), TRIAGE_MODEL)
        assert fake_sdk.calls[0]["max_tokens"] >= 16000

    def test_usage_is_returned_alongside_the_parse(self, fake_sdk):
        _, usage = call(AnthropicClient(), TRIAGE_MODEL)
        assert usage.input_tokens == 10
        assert usage.cache_read_input_tokens == 3


class TestModelSpecificShape:
    def test_triage_stays_lean(self, fake_sdk):
        """The cheap stage must not pay for thinking or effort. `effort` is also
        rejected outright on Haiku 4.5, so sending it would be a hard error."""
        call(AnthropicClient(), TRIAGE_MODEL)
        kwargs = fake_sdk.calls[0]
        assert "thinking" not in kwargs
        assert "output_config" not in kwargs
        assert "betas" not in kwargs

    def test_compile_uses_adaptive_thinking_and_high_effort(self, fake_sdk):
        call(AnthropicClient(), COMPILE_MODEL)
        kwargs = fake_sdk.calls[0]
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "high"}

    def test_no_budget_tokens_anywhere(self, fake_sdk):
        """budget_tokens is removed on Opus 5 and returns a 400 if sent."""
        call(AnthropicClient(), COMPILE_MODEL)
        assert "budget_tokens" not in str(fake_sdk.calls[0])

    def test_compile_enables_server_side_fallback(self, fake_sdk):
        call(AnthropicClient(), COMPILE_MODEL)
        kwargs = fake_sdk.calls[0]
        assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
        assert kwargs["fallbacks"] == "default"


class TestRefusalHandling:
    def test_refusal_raises_rather_than_returning_empty_content(self, fake_sdk):
        """A refusal arrives as HTTP 200 with stop_reason='refusal'. Reading
        .content without checking would yield a silently empty compilation."""
        fake_sdk.stop_reason = "refusal"
        with pytest.raises(RuntimeError, match="declined to compile"):
            call(AnthropicClient(), COMPILE_MODEL)

    def test_refusal_category_is_surfaced(self, fake_sdk):
        fake_sdk.stop_reason = "refusal"
        with pytest.raises(RuntimeError, match="cyber"):
            call(AnthropicClient(), COMPILE_MODEL)

    def test_normal_stop_reason_passes_through(self, fake_sdk):
        fake_sdk.stop_reason = "end_turn"
        parsed, _ = call(AnthropicClient(), COMPILE_MODEL)
        assert parsed.id == "abc-2027"


class TestMissingSdk:
    def test_absent_anthropic_package_gives_an_actionable_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "anthropic", None)
        with pytest.raises(RuntimeError, match=r"pip install"):
            AnthropicClient()
