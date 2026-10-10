"""A deterministic stand-in for the model, for tests and offline demos.

Two reasons this is in the package rather than the test folder. Tests need the
pipeline exercised end to end without spending money or requiring a key, and
anyone cloning the repository should be able to watch the whole flow work before
deciding whether to supply credentials.

Responses are canned per message ref. Anything unrecognised is triaged as noise,
so the stub never silently invents a policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import ProposedAmendment, ProposedArtifact, TriageVerdict


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


# Keyed by the trailing digits of a fixture ref.
_TRIAGE: dict[str, dict[str, Any]] = {
    "000100": dict(actionable=True, kind="policy", confidence=0.95,
                   reason="Mandates an internal registry and digest pinning with a cutoff date."),
    "000101": dict(actionable=False, kind="none", confidence=0.92,
                   reason="A reader asking a clarifying question; imposes nothing."),
    "000102": dict(actionable=False, kind="none", confidence=0.88,
                   reason="Answers a question; restates the existing obligation."),
    "000200": dict(actionable=True, kind="event", confidence=0.96,
                   reason="Planned maintenance window that will break scheduled jobs."),
    "000300": dict(actionable=False, kind="none", confidence=0.80,
                   reason="Process reminder handled elsewhere; nothing in version control changes."),
    "000400": dict(actionable=False, kind="none", confidence=0.99,
                   reason="Office facilities notice."),
    "000500": dict(actionable=True, kind="policy", confidence=0.93,
                   reason="Mandates runAsNonRoot on Kubernetes workloads with an enforcement date."),
    "000600": dict(actionable=False, kind="none", confidence=0.99,
                   reason="Social announcement."),
    "000700": dict(actionable=True, kind="amendment", confidence=0.90,
                   reason="Moves the previously announced cutoff from Jan 15 to Feb 1."),
    "000800": dict(actionable=False, kind="none", confidence=0.97,
                   reason="Meeting logistics."),
}

_COMPILE: dict[str, dict[str, Any]] = {
    "000100": dict(
        kind="policy", id="artifactory-docker-passthrough-2027-01",
        title="Base images must come from the internal Artifactory mirror and be digest-pinned",
        severity="blocking", deadline="2027-01-15", confidence=0.91,
        reasoning="Two distinct obligations in one announcement: registry "
                  "redirection and digest pinning. Globs restricted to Dockerfiles.",
        rules=[
            dict(id="registry-redirect",
                 description="FROM must resolve through artifactory.corp/docker-remote/.",
                 detect=dict(type="file_pattern", globs=["**/Dockerfile", "**/Dockerfile.*"],
                             regex=r"^FROM\s+(?!artifactory\.corp/)"),
                 autofix=dict(type="prefix_rewrite", to="artifactory.corp/docker-remote/"),
                 verify="docker build --pull ."),
            dict(id="digest-pinning",
                 description="Base images must be pinned by digest, not a mutable tag.",
                 detect=dict(type="file_pattern", globs=["**/Dockerfile", "**/Dockerfile.*"],
                             regex=r"^FROM\s+\S+:(latest|\d+\.\d+[\w.-]*)\s*$"),
                 autofix=dict(type="manual",
                              instructions="Resolve the tag with `docker buildx imagetools "
                                           "inspect <image>` and replace it with @sha256:<digest>.")),
        ],
    ),
    "000200": dict(
        kind="event", id="artifactory-upgrade-2037-10-03",
        title="Artifactory proxy unavailable for upgrade",
        severity="high", confidence=0.95,
        window_start="2037-10-03T02:00:00Z", window_end="2037-10-03T06:00:00Z",
        reasoning="Window taken verbatim from the notice. Dependency gate set so "
                  "jobs unrelated to Artifactory are not reported.",
        rules=[
            dict(id="jobs-in-window",
                 description="Scheduled jobs depending on Artifactory that fire during the window.",
                 detect=dict(type="schedule_intersects",
                             globs=[".github/workflows/*.yml", ".github/workflows/*.yaml",
                                    "**/cronjob*.yaml", "**/cronjob*.yml"],
                             depends_on_regex=r"artifactory\.corp"),
                 autofix=dict(type="shift_schedule", by_hours=6)),
        ],
    ),
    "000500": dict(
        kind="policy", id="k8s-run-as-non-root-2026-11",
        title="Kubernetes workloads must set runAsNonRoot",
        severity="blocking", deadline="2026-11-30", confidence=0.87,
        reasoning="Admission controller will reject non-compliant workloads. "
                  "Expressed as an absence rule over Kubernetes manifests.",
        rules=[
            dict(id="run-as-non-root",
                 description="Pod security context must set runAsNonRoot: true.",
                 detect=dict(type="file_pattern",
                             globs=["**/deployment*.yaml", "**/statefulset*.yaml",
                                    "**/cronjob*.yaml"],
                             regex=r"runAsNonRoot:\s*true", absent=True),
                 autofix=dict(type="manual",
                              instructions="Add `runAsNonRoot: true` under "
                                           "spec.template.spec.securityContext."),
                 verify="kubectl apply --dry-run=server -f ."),
        ],
    ),
}


@dataclass
class StubClient:
    """Implements the LLMClient protocol with canned, deterministic responses."""

    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)

    def parse(self, *, model: str, system: str, user: str, schema: type) -> tuple:
        ref = self._ref_from(user)
        self.calls.append((model, ref))
        if ref in self.fail_on:
            raise RuntimeError(f"simulated model failure for {ref}")

        if schema is TriageVerdict:
            spec = _TRIAGE.get(ref, dict(actionable=False, kind="none", confidence=0.5,
                                         reason="unrecognised fixture; treated as noise"))
            return TriageVerdict(ref=ref, **spec), _Usage(1200, 60, 1100, 0)

        if schema is ProposedArtifact:
            spec = _COMPILE.get(ref)
            if spec is None:
                raise RuntimeError(f"stub has no compilation for {ref}")
            return ProposedArtifact(**spec), _Usage(1800, 900, 1500, 0)

        if schema is ProposedAmendment:
            return ProposedAmendment(
                target_id="artifactory-docker-passthrough-2027-01",
                change_summary="Cutoff moved from 2027-01-15 to 2027-02-01.",
                new_deadline="2027-02-01", confidence=0.9,
            ), _Usage(900, 200, 800, 0)

        raise AssertionError(f"stub does not handle schema {schema!r}")

    @staticmethod
    def _ref_from(user: str) -> str:
        for line in user.splitlines():
            if line.startswith("ref:"):
                return line.split("/")[-1].strip()[-6:]
        return "unknown"
