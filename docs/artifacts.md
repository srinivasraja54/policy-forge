# Artifact reference

An **artifact** is the compiled, executable form of an org signal. It is what the
compiler emits, what a human reviews in a pull request, and what the enforcement
engine runs. Everything else in this project exists to produce artifacts or to
act on them.

Artifacts live as one JSON file each under `registry/`, in git. That is
deliberate: git gives review, diffs, blame and rollback for free, and those are
exactly the controls you want around a rule that can open pull requests on other
people's repositories.

## Two kinds

| | `policy` | `event` |
|---|---|---|
| Truth | a durable predicate | a time-bounded fact |
| Example | "no unpinned base images" | "Artifactory down Sat 02:00–06:00 UTC" |
| Ends when | explicitly retired | its window closes, automatically |
| Time field | `deadline` (optional) | `window` (required) |
| Typical detector | `file_pattern` | `schedule_intersects` |

The distinction matters because it decides when enforcement stops. A policy that
nobody retires keeps running forever, which is correct. A maintenance notice that
nobody retires would nag forever, which is how registries rot — so events carry
their own expiry.

## Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | `1` | yes | Bumped only on a breaking schema change. |
| `kind` | `policy` \| `event` | yes | See above. |
| `id` | string | yes | `^[a-z0-9][a-z0-9-]{2,78}$`. Stable forever — the ledger keys on it. |
| `title` | string | yes | One line, human-facing. Appears in briefs and PR bodies. |
| `status` | enum | no | Defaults to `proposed`. See lifecycle below. |
| `severity` | `info`\|`warn`\|`high`\|`blocking` | no | Defaults to `warn`. |
| `source` | object | yes | Provenance. See below. |
| `rules` | array | yes | At least one. Rule ids must be unique within the artifact. |
| `deadline` | date | policy only | Date the policy starts failing CI. |
| `window` | object | event only | `{start, end}`, both timestamps; `end` must follow `start`. |
| `actions` | string[] | no | Free-form hints for downstream handlers, e.g. `reschedule_cron`. |
| `include_repos` | string[] | no | Empty means every repo in the catalog. |
| `exclude_repos` | string[] | no | Wins over `include_repos`. |
| `superseded_by` | string | no | Required when `status` is `superseded`; must resolve to a known id. |
| `amendments` | array | no | Changes applied after first compilation, oldest first. Appended to, never rewritten. See below. |
| `confidence` | 0.0–1.0 | no | The compiler's self-reported confidence. Low values route to review. |
| `created_at` / `updated_at` | timestamp | no | |
| `notes` | string | no | |

Unknown fields are **rejected**, not ignored. A typo like `sevrity` must be an
error — silently dropping it would leave a policy that looks configured and is not.

### `source`

Every artifact must be traceable back to the signal it came from. Without this a
reviewer cannot judge whether the compilation is faithful.

```json
{
  "type": "slack",
  "ref": "C04PLATFORM/p1757600000000100",
  "permalink": "https://example.slack.com/archives/...",
  "captured_at": "2026-09-11T09:14:00Z",
  "excerpt": "Heads up - effective Jan 15, the docker.io passthrough ..."
}
```

`type` is one of `slack`, `postmortem`, `maintenance`, `manual`. `ref` is the
stable dedupe key — the ledger's `processed_sources` table uses it so the same
thread is never recompiled.

## Lifecycle

```
proposed ──(registry PR merged)──► active ──► retired
                                     │
                                     └──► superseded ──► (superseded_by: newer-id)
```

Only `active` artifacts are enforced. This is the review gate: the compiler
writes `proposed`, and a human promoting it to `active` in a pull request is the
single point where model output is checked before it can act on anything.

A follow-up message saying *"we're pushing this to Feb 1"* creates neither a second
artifact nor a `superseded` pair. It is applied to the existing artifact as a diff:
the changed field, plus an `amendments` entry recording what changed it.

`superseded` is reserved for a genuine replacement — a new artifact that takes over
from an old one — and requires `superseded_by`. `retired` is how an amendment
withdraws an obligation.

### `amendments[]`

| Field | Type | Notes |
|---|---|---|
| `source` | object | Same shape as `source` above: the follow-up message, with its excerpt. |
| `summary` | string | What the change did. |
| `applied_at` | timestamp | Also written to the artifact's `updated_at`. |
| `confidence` | 0.0–1.0 | The compiler's confidence in the amendment. |

An amendment can move `deadline` or `window`, change `severity`, or set `status`
to `retired`. It cannot change `id`, `kind`, `rules`, or `source`, and it cannot
set `status` to `active` — those would rewrite what a reviewer already approved.

## Severity and the blocking gate

`blocking` is the only severity that fails `policyctl check` with exit 1. It is
gated on the deadline:

- Policy with `severity: blocking` and a **future** `deadline` → reported, exit 0.
- Same policy once the deadline **arrives** → exit 1.
- `blocking` with **no** deadline → blocks immediately.

That gate is what lets you merge a policy the day it is announced and have it
start gating CI months later without anyone remembering to flip a switch.

## Rules

A single announcement usually carries several distinct obligations, so one
artifact holds several rules.

```json
{
  "id": "registry-redirect",
  "description": "FROM must resolve through artifactory.corp/docker-remote/.",
  "detect": { "type": "file_pattern", "globs": ["**/Dockerfile"],
              "regex": "^FROM\\s+(?!artifactory\\.corp/)" },
  "autofix": { "type": "prefix_rewrite", "to": "artifactory.corp/docker-remote/" },
  "verify": "docker build --pull ."
}
```

`verify` is the **oracle**: a command that must pass for a proposed fix to be
trusted. Where it is set, a remediation can be accepted mechanically rather than
by reading the model's reasoning. Rules without an oracle should prefer
`autofix: {"type": "manual"}` and open an issue instead of a pull request.

## Detectors

### `file_pattern`

Regex over glob-selected files.

| Field | Notes |
|---|---|
| `globs` | At least one. `Dockerfile` and `**/Dockerfile` are both matched. |
| `regex` | Must compile under Python's `re` — see the constraint below. |
| `absent` | When `true`, the violation is the **absence** of a match in matched files. Use for "every Dockerfile must declare a `USER`". |

**Regex constraint.** Two backends implement this detector: ripgrep (`--pcre2`)
when a binary is available, and a pure-Python walk otherwise. They must agree, so
the schema validates every pattern by compiling it with Python's `re` at
authoring time. PCRE2-only syntax — atomic groups, recursion, variable-length
lookbehind — is therefore rejected when the artifact is written, rather than
failing later on whichever machines happen to lack ripgrep. Lookahead, used by
the example above, is supported by both.

Run `policyctl check` and read the `search backend:` line to see which is active.
Point `$POLICYFORGE_RG` at a binary if `rg` on your system is a shell function
rather than an executable.

### `schedule_intersects`

Finds scheduled jobs that fire inside an event's window. Parses `cron` entries
from GitHub Actions workflows and `spec.schedule` from Kubernetes CronJobs.

| Field | Notes |
|---|---|
| `globs` | Defaults to Actions workflows plus `**/cronjob*.y[a]ml`. |
| `depends_on_regex` | Optional gate. Only flags jobs in files that also match this. |

`depends_on_regex` is what keeps the signal honest. A repo may well have a job
firing at 02:30 during an Artifactory maintenance window while having nothing to
do with Artifactory; reporting it is a false positive, and false positives are
what get tools like this switched off.

GitHub Actions cron is always UTC. Kubernetes CronJob is UTC unless
`spec.timeZone` says otherwise, which is honoured.

## Autofixes

| Type | Effect |
|---|---|
| `prefix_rewrite` | Prepend `to` right where the rule's own `detect.regex` stopped matching. |
| `regex_replace` | `pattern` → `replacement`, its own pattern, not necessarily the detector's. |
| `shift_schedule` | Move a cron by `by_hours`, optionally reverting after `revert_after`. Not yet automated — see below. |
| `manual` | No safe mechanical fix; `instructions` become the issue body. |

Choosing `manual` is a legitimate and often correct outcome. A fix that cannot be
verified should be described to a human, not guessed at.

### Applying an autofix: `policyctl remediate`

`prefix_rewrite` and `regex_replace` are computed by `policyforge.remediate`, with
no model call: the fix is a mechanical consequence of `detect.regex` finding the
violation in the first place, never a second, independent guess at what it means.
Two safeguards keep that honest:

- **The fix must clear its own detector.** After applying it, the rule's
  `detect.regex` is re-run against the result; if it still matches, the fix is
  refused rather than written. This is a free oracle — no `verify` command needed
  to catch a `prefix_rewrite`/`detect.regex` pair that disagree with each other.
- **A matched reference that is itself a build variable** (`$BASE_IMAGE`,
  `${BASE_IMAGE}`) is refused for `prefix_rewrite`, never mechanically rewritten.
  Prepending a mirror prefix there would silently break the interpolation rather
  than fix it — exactly the `FROM $BASE_IMAGE` case called out earlier in this
  repo's own fixtures.

A computed fix only ever reaches a repository once `rule.verify` is set **and**
passes, run against a throwaway copy of the repo first. Two ways to land it from
there, both gated by that same pass:

- `--write` applies it directly to the real local checkout.
- `--pr` pushes it to a dedicated branch (`policyctl/<artifact.id>/<rule.id>`,
  force-pushed — the branch is understood to belong entirely to this tool) in a
  throwaway clone and opens a PR, using the checkout's own `origin` remote to
  know which repo and `GITHUB_TOKEN` to authenticate. Idempotent per
  (artifact, rule, repo) via the ledger's `actions` table: a fix already opened
  as a PR is skipped on a later run, not reopened.

A rule with no `verify` stays diff-only regardless of either flag: without that
oracle there is nothing to trust over a human's own reading of the diff, the
same principle that already governs the choice of `manual` above.

`shift_schedule` (for `schedule_intersects` rules) is not yet wired up to
`policyctl remediate` — rewriting a cron expression safely needs YAML-aware
editing, not line regex, and is a planned follow-up.

## Authoring by hand

Artifacts do not have to come from the compiler. To write one directly:

```bash
$EDITOR registry/policies/my-policy.json
policyctl validate          # schema, unique ids, resolvable superseded_by
policyctl check --no-record # see what it would flag before setting status: active
```

Start at `status: proposed` and confirm the detection is right before promoting
it. An over-broad regex promoted straight to `active` opens pull requests on
every repo you own.
