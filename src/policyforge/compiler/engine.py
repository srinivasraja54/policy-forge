"""The two-stage compilation engine.

Stage 1 (triage, Haiku 4.5) reads every message and passes through the small
fraction that impose a checkable obligation. Stage 2 (compile, Opus 5) runs only
on survivors. The funnel is the cost argument: ~95% of channel traffic never
reaches the expensive model, and the expensive model runs once per announcement
rather than once per (announcement x repository).

The client is injected rather than constructed here so the pipeline, the CLI and
the tests can all run against a stub with no API key and no network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from ..sources.base import Message
from .models import ProposedAmendment, ProposedArtifact, TriageVerdict
from .prompts import AMENDMENT_SYSTEM, COMPILE_SYSTEM, TRIAGE_SYSTEM

log = logging.getLogger(__name__)

TRIAGE_MODEL = "claude-haiku-4-5"
COMPILE_MODEL = "claude-opus-5"

# Announcements are short. Capping input keeps a pasted log dump from turning one
# message into a large bill.
MAX_MESSAGE_CHARS = 4000


@dataclass
class Usage:
    """Token accounting, surfaced so the cost claims in the README stay honest."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    triage_calls: int = 0
    compile_calls: int = 0

    def add(self, usage, *, stage: str) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        if stage == "triage":
            self.triage_calls += 1
        else:
            self.compile_calls += 1

    def estimated_cost_usd(self) -> float:
        """Rough, for the run summary only. Triage and compile are billed at
        different rates; this attributes cached reads at the cheaper rate."""
        compile_in = self.input_tokens * 5.00 / 1_000_000
        compile_out = self.output_tokens * 25.00 / 1_000_000
        cached = self.cache_read * 0.50 / 1_000_000
        return round(compile_in + compile_out + cached, 4)


class LLMClient(Protocol):
    """The surface the engine needs. Implemented by AnthropicClient and StubClient."""

    def parse(self, *, model: str, system: str, user: str, schema: type) -> tuple:
        """Return (parsed_instance, usage)."""
        ...


class AnthropicClient:
    """Real client. Imported lazily so the package works without `anthropic`."""

    def __init__(self, api_key: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised by install docs
            raise RuntimeError(
                "the compile stage needs the anthropic SDK: pip install '.[compile]'"
            ) from exc
        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def parse(self, *, model: str, system: str, user: str, schema: type) -> tuple:
        kwargs = dict(
            model=model,
            max_tokens=16000,
            # The system prompt is the stable prefix. Marking it here is what
            # makes repeat runs cheap; see prompts.py for why it must not vary.
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        if model == COMPILE_MODEL:
            # Adaptive thinking is on by default on Opus 5; effort is the lever
            # that matters. Compilation is judgement-heavy, so it earns `high`.
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": "high"}
            # Server-side fallback: a refusal returns HTTP 200 with
            # stop_reason="refusal", and routing by category avoids maintaining
            # a model list by hand.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            response = self._client.beta.messages.parse(**kwargs)
        else:
            response = self._client.messages.parse(**kwargs)

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(
                f"model declined to compile this message "
                f"(category={getattr(details, 'category', 'unknown')})"
            )
        return response.parsed_output, response.usage


@dataclass
class Engine:
    client: LLMClient
    usage: Usage = field(default_factory=Usage)

    # ---- stage 1 ---------------------------------------------------------

    def triage(self, message: Message) -> TriageVerdict:
        user = _triage_user_turn(message)
        verdict, usage = self.client.parse(
            model=TRIAGE_MODEL, system=TRIAGE_SYSTEM, user=user, schema=TriageVerdict
        )
        self.usage.add(usage, stage="triage")
        # The model echoes the ref back; trust our own value over its copy so a
        # transcription slip cannot misattribute a verdict to another message.
        return verdict.model_copy(update={"ref": message.ref})

    # ---- stage 2 ---------------------------------------------------------

    def compile(self, message: Message) -> ProposedArtifact:
        user = _compile_user_turn(message)
        proposal, usage = self.client.parse(
            model=COMPILE_MODEL, system=COMPILE_SYSTEM, user=user,
            schema=ProposedArtifact,
        )
        self.usage.add(usage, stage="compile")
        return proposal

    def amend(self, message: Message, existing_artifact_json: str) -> ProposedAmendment:
        user = (
            f"## Existing artifact\n\n```json\n{existing_artifact_json}\n```\n\n"
            f"## Follow-up message\n\n{_message_block(message)}"
        )
        amendment, usage = self.client.parse(
            model=COMPILE_MODEL, system=AMENDMENT_SYSTEM, user=user,
            schema=ProposedAmendment,
        )
        self.usage.add(usage, stage="compile")
        return amendment


def _message_block(message: Message) -> str:
    lines = [
        f"ref: {message.ref}",
        f"posted_at: {message.posted_at.isoformat()}",
        f"channel: {message.channel or 'unknown'}",
        f"author: {message.author or 'unknown'}",
    ]
    if message.thread_parent:
        lines.append(f"in_reply_to: {message.thread_parent}")
    body = message.truncated(MAX_MESSAGE_CHARS)
    return "\n".join(lines) + f"\n\n---\n{body}\n---"


def _triage_user_turn(message: Message) -> str:
    return f"Classify this message.\n\n{_message_block(message)}"


def _compile_user_turn(message: Message) -> str:
    return (
        "Compile this announcement into a policy artifact.\n\n"
        f"{_message_block(message)}"
    )
