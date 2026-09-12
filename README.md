# policy-forge

**Compiles unstructured org signals into executable policy, then enforces it deterministically.**

Platform teams announce governance in prose — a Slack post, a postmortem, a maintenance notice. Downstream teams then read that prose and manually work out whether it affects their repos. That translation step is the expensive, unautomated part, and it is the only part this project asks a language model to do.

Everything after it is pattern matching and calendar arithmetic — no model calls at all.

---

## The idea

Existing tools — OpsLevel Campaigns, Cortex Scorecards, Spotify Soundcheck — all begin *after* a human has hand-authored a machine-checkable rule. policy-forge automates the authoring step and leaves enforcement deterministic:

```
Slack announcement  ─┐
Postmortem          ─┼─► compile (LLM, once) ─► policy artifact ─► human review (PR)
Maintenance notice  ─┘                              │
                                                    ▼
                                   enforce (pattern + cron math, zero tokens)
                                                    │
                                       ┌────────────┼────────────┐
                                       ▼            ▼            ▼
                                  draft PRs     ledger      per-team brief
```

**Compile once, apply N times.** The model runs once per announcement, not once per (announcement × repo). The artifact it produces then runs against every repo for free, forever — including repos created months later. That collapses `O(A×R)` model calls to `O(A)`.

## Two artifact kinds

| | `policy` | `event` |
|---|---|---|
| Truth | durable predicate | time-bounded fact |
| Example | "no unpinned base images" | "Artifactory down Sat 02:00–06:00 UTC" |
| Expires | when retired | automatically, at window end |
| Detector | regex over globs | cron intersection |

## What it looks like

An announcement lands in `#platform-announcements`:

> Heads up — effective **Jan 15**, the `docker.io` passthrough on our Artifactory proxy is shut off (SLSA L2). All Dockerfiles must pull base images from `artifactory.corp/docker-remote/`. Also, unpinned tags like `FROM ubuntu:latest` are no longer permitted — pin by digest.

One message, two obligations, a deadline. It compiles to [`registry/policies/artifactory-docker-passthrough-2027-01.json`](registry/policies/artifactory-docker-passthrough-2027-01.json) — reviewed as a PR before it can act on anything — and then:

```console
$ policyctl check

artifactory-upgrade-2026-10-03 event/high
  violating   payments-api   jobs-in-window   n=1
      .github/workflows/nightly.yml:4  0 3 * * *
        nightly-build fires in window: 2026-10-03 03:00 UTC
  violating   data-platform  jobs-in-window   n=2
      .github/workflows/retrain.yml:4  0 4 * * 6
        weekly-retrain fires in window: 2026-10-03 04:00 UTC
  clear       web-ui         jobs-in-window

artifactory-docker-passthrough-2027-01 policy/blocking
  violating   payments-api   registry-redirect  n=2
      Dockerfile:1  FROM ubuntu:latest
      services/ml-trainer/Dockerfile:2  FROM $BASE_IMAGE
  clear       web-ui         registry-redirect
```

Two details worth noting, both produced with **zero model calls**:

- `web-ui` has a cron at `30 2 * * *` that *does* fire inside the maintenance window — and is correctly not flagged, because it has no Artifactory dependency.
- `FROM $BASE_IMAGE` is surfaced as a case a human must resolve, rather than silently mis-fixed.

## Design principle: every LLM output has a deterministic oracle

The model proposes; a cheap, deterministic verifier disposes.

| Stage | Oracle |
|---|---|
| Policy rule | pattern match + the repo's own build |
| Remediation diff | `verify` command must pass |
| Postmortem fix | regression test fails before, passes after |
| Maintenance impact | cron expression intersects the window |

Where that oracle exists, the model can be creative and expensive because verification is free and certain. Where it does not, the tool dies of review fatigue. This is the project's actual thesis.

## Cost

The naive design — feed 24h of Slack plus repo contents to a large model nightly — costs hundreds of dollars a month and scales with repo count. This design does not:

| Lever | Effect |
|---|---|
| Haiku triage before Opus compile | ~95% of messages never reach the expensive model |
| Compile once, apply N times | `O(A×R)` → `O(A)` model calls |
| Detection via pattern matching | repo scanning costs nothing |
| Ledger dedupe | follow-ups are a lookup, not a model call |
| Prompt caching | stable prefix at 10% of input price |
| Batch API for the nightly job | 50% off; latency is irrelevant to cron |

## Status

- [x] **Phase 1** — artifact schema, registry, pattern + cron detectors, ledger, `policyctl`
      <br>_103 tests, 97% coverage. See [docs/artifacts.md](docs/artifacts.md) for the artifact schema._
- [ ] **Phase 2** — the compiler (Haiku triage → Opus compile, structured outputs, eval set)
- [ ] **Phase 3** — Slack ingestion
- [ ] **Phase 4** — GitHub output (draft PRs, issues)
- [ ] **Phase 5** — FastAPI: burndown, team brief, Slack query endpoint
- [ ] **Phase 6** — GitHub Actions: nightly compile, enforce-on-push

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

policyctl validate          # validate every artifact in the registry
policyctl check             # enforce against the fixture repos — no API key needed
policyctl report            # compliance burndown
policyctl brief payments    # one morning digest for a team
```

`policyctl check` prints which search backend it used. It prefers ripgrep and falls
back to a pure-Python walk when no binary is found — set `$POLICYFORGE_RG` if `rg` on
your system is a shell function rather than an executable. Both backends are held to
the same semantics by a parity test suite.

`policyctl check` needs no API key, no network, and no service. That is what makes it affordable to run as a required status check on every pull request — which is the difference between preventing violations and mopping them up forever.

## Layout

```
src/policyforge/
  schema.py        artifact model: kinds, lifecycle, rules, detectors, autofixes
  registry.py      git-backed artifact store + repo catalog
  ledger.py        SQLite: audit history and action idempotency
  detect/
    file_pattern.py   regex over globs (ripgrep when available, Python fallback)
    schedule.py       cron × maintenance-window intersection
  cli.py           policyctl
docs/artifacts.md  artifact schema reference — read this to author one by hand
registry/          the artifacts themselves — reviewed as PRs
fixtures/repos/    sample repos the engine runs against
```
