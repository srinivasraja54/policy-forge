# Compilation

Turning an announcement into an executable artifact is the only step in this
project that uses a language model. Everything downstream is deterministic. This
document covers how that step works, what it costs, and where it is checked.

## Two stages

```
message ──► ledger dedupe ──► triage (Haiku 4.5) ──► compile (Opus 5) ──► artifact
             (SQLite)          ~95% stop here        one call per         status:
             free              cheap                 announcement         proposed
```

The order is the cost design:

1. **Dedupe first.** A message whose `ref` is already in `processed_sources` costs
   one SQLite lookup. Nothing else runs.
2. **Triage second.** A cheap model decides whether the message imposes an
   obligation that could be checked mechanically. Most channel traffic — welcomes,
   meeting changes, questions, facilities notices — stops here.
3. **Compile last.** The expensive model runs once per surviving announcement, and
   **never** once per repository. That is what keeps cost independent of org size.

On the bundled fixtures — ten messages representing a day of channel traffic —
that is ten cheap calls and three expensive ones.

## What each stage emits

Both stages use structured outputs, so the response is schema-valid JSON rather
than prose that has to be parsed.

| Stage | Model | Output type | Thinking / effort |
|---|---|---|---|
| Triage | `claude-haiku-4-5` | `TriageVerdict` | none — the cheap stage must not pay for it, and `effort` is rejected on Haiku 4.5 |
| Compile | `claude-opus-5` | `ProposedArtifact` | `thinking: adaptive`, `effort: high` |
| Amend | `claude-opus-5` | `ProposedAmendment` | same as compile |

`TriageVerdict.kind` is one of `policy`, `event`, `amendment`, or `none`. The
first two route to compilation; `amendment` routes to the amendment path;
`none` is recorded as noise so it is never re-evaluated.

## The conversion boundary

The model does not write artifacts. It writes a **proposal**, which
`to_artifact()` converts and our own schema validates. That boundary is where
several things are taken out of the model's hands:

| Field | Who decides | Why |
|---|---|---|
| `status` | **us** — always `proposed` | A model that could emit `active` would be self-approving. |
| `source` | **us** — rebuilt from the message | Provenance must describe the message we actually read, not what the model recalls of it. |
| `id` | model, then slugified and collision-suffixed | The ledger keys on it, so it must be well-formed and unique. |
| `rules`, `severity`, `deadline` | model | This is the actual judgement work. |
| `confidence` | model | Routes review attention. Below `--min-confidence` (default 0.4) the proposal is rejected rather than written. |

A proposal that is valid JSON but a bad artifact — a `file_pattern` rule with no
regex, an `event` with no window, a regex that does not compile, an unusable id —
is rejected here with a reason, not written and not silently dropped.

## The review gate

Compiled artifacts are written as `status: proposed`, and
`policyctl check` ignores anything that is not `active`. Promotion happens when a
human merges the registry pull request.

This is the single place where model output is checked before it can act on
another team's repository, which is why it is a git review rather than a
confidence threshold. A threshold sorts proposals; it does not read them.

```bash
policyctl compile --stub          # writes status: proposed
policyctl check                   # "no enforceable artifacts" — nothing is active
# review the diff, set status to "active", merge
policyctl check                   # now it enforces
```

## Amendments

A follow-up that moves a deadline is neither a new artifact nor noise.

- Compiling it fresh leaves two contradictory policies active.
- Skipping it as "just a reply" lets the registry quietly go stale, which is worse
  than not having the policy at all — it looks maintained and is not.

So triage classifies it as `amendment` and it becomes a diff against the artifact
it targets. In git that is a reviewable two-line change with full blame.

Resolving *which* artifact a follow-up targets needs the thread parent, which
arrives with Phase 3. Until then amendments are surfaced in the report and left
for a human rather than being applied or dropped.

## Cost controls

| Control | Where | Effect |
|---|---|---|
| Ledger dedupe | `Pipeline._handle` | Seen refs never reach a model. |
| Triage funnel | stage 1 | Most traffic never reaches the expensive model. |
| Compile once per announcement | stage 2 | Cost is independent of repo count. |
| Prompt caching | `prompts.py` + `cache_control` | Stable system prefix served at ~10% of input price. |
| Input truncation | `Message.truncated()` | A pasted log dump cannot become a large bill. |

**Prompt caching is a prefix match**, so `prompts.py` must stay byte-stable. A
`datetime.now()` interpolated into a system prompt, or a dict serialised in
varying key order, silently drops the hit rate to zero. Everything that varies
per request belongs in the user turn, after the last breakpoint. `policyctl
compile` prints `cache read` in its run summary — if that figure is zero across
repeated runs, something in the prefix is moving.

## Running it for real

```bash
pip install -e ".[compile]"
export ANTHROPIC_API_KEY=sk-ant-...
policyctl compile --source fixtures/slack --dry-run    # see it without writing
policyctl compile --source fixtures/slack
```

Useful flags: `--limit N` caps how many messages are processed, `--dry-run`
writes nothing and does not mark messages as seen, and `--min-confidence` raises
the bar for what gets written.

The compile stage enables server-side fallback
(`betas: ["server-side-fallback-2026-07-01"]`, `fallbacks: "default"`), so a
refusal is routed by category rather than failing the run. A refusal that still
arrives is raised with its category rather than being read as an empty
compilation — a refusal returns HTTP 200, so code that reads `.content` without
checking `stop_reason` would otherwise record a silent success.

## Adding a source

`SourceAdapter` is a `Protocol` with one method. Slack, postmortems and
maintenance notices are different *inputs* to one compiler, not three systems:

```python
class MySource:
    name = "postmortems"

    def fetch(self, since: datetime) -> Iterable[Message]:
        ...
```

`Message.ref` must be stable across runs — it is the dedupe key. Set
`thread_parent` on replies so amendment routing can find the artifact they target.
