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

artifactory-upgrade-2037-10-03 event/high
  violating   payments-api   jobs-in-window   n=1
      .github/workflows/nightly.yml:4  0 3 * * *
        nightly-build fires in window: 2037-10-03 03:00 UTC
  violating   data-platform  jobs-in-window   n=2
      .github/workflows/retrain.yml:4  0 4 * * 6
        weekly-retrain fires in window: 2037-10-03 04:00 UTC
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

## The funnel, measured

`policyctl compile` over a day of channel traffic. Ten messages: one deprecation
announcement, one maintenance notice, one security mandate, a deadline change, two
thread replies, and four pieces of ordinary channel noise.

```console
$ policyctl compile --stub

 ref        disposition   artifact                              why
 00000100   compiled      artifactory-docker-passthrough-2027…  Two distinct obligations in one
                                                                announcement: registry redirection
                                                                and digest pinning.
 00000101   noise                                               A reader asking a clarifying
                                                                question; imposes nothing.
 00000200   compiled      artifactory-upgrade-2037-10-03        Window taken verbatim. Dependency
                                                                gate set so unrelated jobs are not
                                                                reported.
 00000400   noise                                               Office facilities notice.
 00000500   compiled      k8s-run-as-non-root-2026-11           Admission controller will reject
                                                                non-compliant workloads.
 00000700   amendment     artifactory-docker-passthrough-2027…  Cutoff moved from 2027-01-15 to
                                                                2027-02-01. [deadline]

10 message(s): 3 compiled, 1 amendment, 6 noise, 0 duplicate, 0 rejected, 0 error
10 triage call(s), 4 compile call(s)

wrote 3 artifact(s) as status: proposed

amended 1 existing artifact(s):
  registry/policies/artifactory-docker-passthrough-2027-01.json
```

**Ten cheap calls, four expensive ones.** That ratio is the whole cost argument,
and it holds regardless of how many repositories the org has — compilation happens
once per announcement, never once per announcement × repo.

Three further things in that output are load-bearing:

- **Everything lands as `proposed`.** `policyctl check` ignores it entirely until a
  human promotes it to `active` in the registry pull request. That review is the
  only gate between model output and action on someone's repository, and the model
  cannot open it — `to_artifact()` overwrites `status` and rebuilds provenance from
  the message we actually read, so a proposal claiming `active` gets neither.
- **The deadline change is applied as an `amendment`, not a new artifact.** Compiling
  it fresh would leave two contradictory policies active; dropping it as "just a
  reply" is how a registry silently goes stale. Instead it rewrites the artifact it
  targets, in place — and *which* artifact that is comes from the thread, not from
  the model: the parent announcement's ref is already recorded as that artifact's
  `source.ref`, so the target is a dict lookup against the registry — no database,
  durable with nothing but the git-tracked artifact file. The model is shown that
  one artifact and asked only what changes. In git the result is the moved deadline
  plus an `amendments` entry carrying the message that moved it, because a deadline
  that no longer matches the excerpt it was compiled from is misleading on its own.
- **Re-running recompiles nothing that already landed.** A ref already represented
  in the registry — compiled or amended — is a dict lookup, so the expensive stage
  never repeats. Noise isn't remembered this way (nothing is written for it), so
  it's triaged again if seen twice; that costs one cheap call, not zero.

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
| Registry dedupe | an already-compiled or -amended ref is a dict lookup, no database — durable across CI runs for free |
| Prompt caching | stable prefix at 10% of input price |
| Batch API for the nightly job | 50% off; latency is irrelevant to cron — *planned, Phase 6* |

## Status

- [x] **Phase 1** — artifact schema, registry, pattern + cron detectors, ledger, `policyctl`
- [x] **Phase 2** — the compiler: Haiku triage → Opus compile, structured outputs, review gate
- [x] **Phase 2.5** — amendments applied, not just reported: thread-resolved targets, provenance trail
  - dedupe and resolution now read entirely off the registry (`Registry.source_refs()`) — no ledger, no database; durable across a fresh CI checkout where a `.gitignore`d `ledger.sqlite` would not be
- [x] **Phase 3** — Slack ingestion: `policyctl compile --slack` reads real channel history and threads through the same `SourceAdapter` seam `FixtureSource` always used. Unit-tested against a mocked Slack API (`httpx.MockTransport`, no network in CI) *and* verified against a live workspace: a real announcement posted to a real channel was fetched, triaged and compiled by the real model, promoted to `active`, and correctly flagged real violations via `policyctl check` — the whole loop, no stub anywhere.
- [~] **Phase 4** — GitHub output (draft PRs, issues)
  - [x] autofix applier + `verify` runner: `policyctl remediate` computes a diff from the rule's own `detect.regex`, runs `verify` in a throwaway copy of the repo, and writes only what passed — no model calls
  - [x] GitHub client: `policyctl remediate --pr` pushes a verified fix to a dedicated branch in a throwaway clone and opens a draft PR — the target's own git remote decides which repo, no new catalog field needed. Idempotent via the ledger's `claim_action`/`attach_url` (built since Phase 1, finally has a caller). Unit-tested against a real local git repo plus a mocked GitHub API (`httpx.MockTransport`, no network in CI), and verified live twice over: once run manually from the terminal, once as a fully automated GitHub Actions workflow — a real `git push` to a separate test repo triggered `policyctl check`, its failure triggered `policyctl remediate --pr`, and a real draft PR landed, with zero human intervention end to end.
  - [ ] Issues: `docs/artifacts.md` already documents that a `manual` autofix's `instructions` are meant to become "the issue body" — opening that issue isn't built; a `manual` rule is still reported to the terminal only.
- [ ] **Phase 5** — FastAPI: burndown, team brief, Slack query endpoint
- [~] **Phase 6** — GitHub Actions: `.github/workflows/` has `ci.yml` (tests, runs today), `enforce.yml` (enforce-on-push against the bundled fixtures, zero model calls, runs today), and `compile.yml` (nightly `policyctl compile --slack` → PR; needs `ANTHROPIC_API_KEY` + `SLACK_BOT_TOKEN` as repo secrets). Drafted, not yet verified against a live run. See [`docs/workflows.md`](docs/workflows.md), which also documents the template for enforcing this registry in a separate, downstream repo.

_299 tests, 97% coverage. Reference: [artifact schema](docs/artifacts.md) · [compilation](docs/compiling.md) · [workflows](docs/workflows.md)._

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

policyctl compile --stub    # compile fixture announcements — no API key needed
policyctl compile --slack   # compile real Slack channel history — needs SLACK_BOT_TOKEN
policyctl validate          # validate every artifact in the registry
policyctl check             # enforce against the fixture repos
policyctl remediate         # compute + verify fixes for violations with an autofix
policyctl report            # compliance burndown
policyctl brief <team's channel name>    # one morning digest for a team
```

`--stub` runs the whole compilation pipeline against canned model responses, so you
can watch the full flow before deciding whether to supply credentials. To run it for
real:

```bash
pip install -e ".[dev,compile]"   # adds the anthropic SDK + python-dotenv
cp .env.example .env              # fill in ANTHROPIC_API_KEY
policyctl compile                 # .env is loaded automatically; drop --stub
```

`.env` is only ever read on this path — `check` and `validate` need no key, no
network, and no service, and nothing about them changes whether or not `.env`
exists.

Reading real Slack channels instead of the bundled fixture:

```bash
pip install -e ".[dev,slack]"      # adds httpx + python-dotenv
# .env: SLACK_BOT_TOKEN=xoxb-..., SLACK_CHANNEL_IDS=C01234567,C07654321
policyctl compile --slack --stub   # real channel traffic, canned model responses --
                                    # the way to smoke-test the connection for free
policyctl compile --slack          # the real thing
```

The bot needs `channels:history` (or `groups:history`), and to actually be a
member of each configured channel — Slack returns no history for a channel a
bot hasn't joined. `users:read` and `team:read` are optional: without them, a
message's author falls back to its raw Slack user id and its `permalink` is
left unset, both quietly, never a failed run.

`policyctl check` prints which search backend it used. It prefers ripgrep and falls
back to a pure-Python walk when no binary is found — set `$POLICYFORGE_RG` if `rg` on
your system is a shell function rather than an executable. Both backends are held to
the same semantics by a parity test suite.

`policyctl check` needs no API key, no network, and no service. That is what makes it affordable to run as a required status check on every pull request — which is the difference between preventing violations and mopping them up forever.

`policyctl remediate` is dry-run by default: it computes a diff and, where the rule
sets a `verify` command, runs it in a throwaway copy of the repo — never the real
checkout. `--write` applies a fix locally only once that verify has passed; `--pr`
instead pushes it to a dedicated branch in a throwaway clone and opens a draft PR
(needs `POLICYFORGE_GITHUB_TOKEN` — the target's own git remote decides which repo). A rule
with no `verify` stays diff-only regardless of either flag; there is no flag that
skips the oracle.

## Layout

```
src/policyforge/
  schema.py        artifact model: kinds, lifecycle, rules, detectors, autofixes
  registry.py      git-backed artifact store + repo catalog
  ledger.py        SQLite: audit history and action idempotency
  detect/
    file_pattern.py   regex over globs (ripgrep when available, Python fallback)
    schedule.py       cron × maintenance-window intersection
  compiler/
    engine.py         three model stages (triage, compile, amend), injectable client
    pipeline.py       orchestration + the proposal→artifact validation boundary,
                      and the thread→target resolution amendments run on
    prompts.py        byte-stable system prompts (the cached prefix)
    stub.py           canned responses, so the pipeline runs with no API key
  sources/
    base.py           SourceAdapter protocol — Slack/postmortem/maintenance all
    fixture.py        reduce to one Message shape
    slack.py          the real Web API, reduced to the same shape
  remediate/
    applier.py        Autofix + Finding → FileChange, from the rule's own regex
    verify.py         run `verify` in a throwaway copy of the repo — the oracle
    github.py         push a verified fix to a branch, open (or reuse) a PR
  cli.py           policyctl
docs/artifacts.md  artifact schema reference — read this to author one by hand
docs/workflows.md  what each GitHub Action does, and the downstream-repo template
registry/          the artifacts themselves — reviewed as PRs
fixtures/repos/    sample repos the engine runs against
.github/workflows/ ci.yml, enforce.yml, compile.yml — see docs/workflows.md
```
