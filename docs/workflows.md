# GitHub Actions

Three workflows ship in `.github/workflows/`. Two run today, on this repo, with
no setup. The third needs two secrets and a repo variable. A fourth pattern —
for a *downstream* repo that wants policy-forge's registry enforced against its
own code — is documented at the bottom as a template, not a running workflow,
because it names a repo (`<org>/policy-forge`) that only exists once you've
published one.

## `ci.yml` — the project's own tests

Push or PR to this repo: install, `pytest`, `policyctl validate`. No API key,
no network, no secrets. This is the one that should pass on a PR from a fork
with zero configuration.

## `enforce.yml` — enforce-on-push

Runs `policyctl check --no-record` against the registry and fixture repos
bundled in this repo. Zero model calls, zero network, which is the entire
argument for running this on *every* push rather than as a nightly scan —
prevention is affordable exactly because this costs nothing.

This is also the pattern a real deployment copies into each repo it protects:
point `--root` at that repo's own checkout and `--repo` at its name in
`catalog.yaml`, and the command is identical. See the template at the bottom.

## `compile.yml` — the nightly, expensive half

The only workflow that spends tokens or calls a third party. On a schedule (and
on `workflow_dispatch` for a manual backfill), it reads recent Slack
announcements, compiles whatever is actionable, and opens one PR with
everything it wrote — `status: proposed`, so nothing it writes is enforced
until a human merges it.

**The `schedule:` trigger ships commented out.** Until the secrets below are
configured, a nightly firing would just fail at `AnthropicClient()` construction
— cheap (no model call, no spend) but a pointless red run every night. Add the
secrets, then uncomment the `cron:` line; `workflow_dispatch` works immediately
for a manual test run regardless.

**Secrets** (repo settings → Secrets and variables → Actions):

| Name | Kind | Used for |
|---|---|---|
| `ANTHROPIC_API_KEY` | secret | the compiler |
| `SLACK_BOT_TOKEN` | secret | reading channel history |
| `SLACK_CHANNEL_IDS` | **variable**, not secret | which channels to read |

The bot token needs `channels:history` (or `groups:history` for private
channels), `users:read` (for a display name instead of a raw user id in the
compile prompt — optional, falls back cleanly), and `team:read` (for a real
permalink in each artifact's `source.permalink` — also optional, falls back to
no permalink). It also needs to actually be a member of each configured
channel; Slack does not return history for channels a bot hasn't joined.

**Why `--since-days 2` on a daily cron:** dedupe is durable via the registry
itself (`Registry.source_refs()` — see `docs/compiling.md`), not a side
database, so a window wider than the schedule interval costs nothing: an
already-compiled or -amended message is a dict lookup away from being skipped,
on every run, including the first one after this workflow was down for a day.
Narrowing it back to match the schedule exactly would only reintroduce the
failure mode a wider window is free to avoid.

**Why one fixed branch (`policyctl/compile`):** `peter-evans/create-pull-request`
updates the existing open PR on that branch rather than opening a new one every
night, so a backlog of reviewable artifacts does not turn into a backlog of
redundant PRs.

## Template: enforcing policy-forge's registry in a downstream repo

Not a file in this repo's `.github/workflows/` — it names a placeholder
(`<org>/policy-forge`) and would not run as shipped. Copy it into a downstream
repo's `.github/workflows/` and fill in the real repository.

policy-forge is not yet published as a package, so the simplest working form
checks out the registry alongside the downstream repo's own code:

```yaml
name: Policy enforcement

on:
  push:
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          path: repo

      - uses: actions/checkout@v4
        with:
          repository: <org>/policy-forge
          path: policy-forge
          # token: ${{ secrets.POLICY_FORGE_READ_TOKEN }}  # if it's private

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install policyctl
        run: pip install -e ./policy-forge

      - name: Enforce active policy
        run: |
          policyctl check \
            --registry policy-forge/registry \
            --root repo \
            --repo "$(basename "$GITHUB_REPOSITORY")"
```

Two things to get right that are easy to miss:

- **`catalog.yaml`'s entry for this repo needs `path: .`** when deployed this
  way. The bundled demo catalog points `path` at `fixtures/repos/payments-api`
  because every fixture repo lives inside *this* checkout; a real downstream
  repo checks out only itself, so its own entry should resolve to the checkout
  root, not a subdirectory.
- **`--repo` must match the catalog entry's `name` exactly**, or `check` finds
  no target at all and prints `no targets to check` with exit `0` — not a
  failure, which is deliberate (a repo not yet onboarded in `catalog.yaml` is
  not an error), but worth knowing when a required check mysteriously reports
  nothing to check rather than failing loudly on a typo.

Opening a *fix* as a PR in the downstream repo — rather than only failing the
check — is `policyctl remediate --pr`'s job, with the same two-checkout shape:
```bash
policyctl remediate --registry policy-forge/registry --root repo \
  --repo "$(basename "$GITHUB_REPOSITORY")" --pr
```
It needs `POLICYFORGE_GITHUB_TOKEN` (see `.env.example`) and nothing else repo-specific —
the target's own `origin` remote decides which GitHub repo the PR opens
against, read straight from the checkout at `repo/`. See
[`docs/artifacts.md`](artifacts.md#applying-an-autofix-policyctl-remediate) for
how a fix is computed and verified before it ever reaches this step, and the
module docstring in `src/policyforge/remediate/github.py` for how idempotency
works (one branch per rule, force-pushed; the ledger's `actions` table is what
stops a second run opening a second PR for the same fix *within one checkout
with a persistent ledger* -- a workflow whose runner is ephemeral, like
GitHub Actions, gets a fresh empty ledger every run, so that fast path never
engages there. It stays correct anyway: `open_fix_pr` checks GitHub itself for
an existing open PR on the branch before creating one, so repeated runs
update the same PR rather than opening a new one -- just without skipping the
git work to get there. Worth tightening later (cache the ledger file between
runs) but not a correctness problem today.

This exact pattern -- `check` on push, and on failure `remediate --pr` -- is
proven, not just documented: it's the live workflow running in
`policy-forge-test-repo`, where a real push triggered a real failing `check`,
which triggered a real `remediate --pr`, which opened a real draft PR, with no
human in the loop. That repo also shows the one adaptation a real deployment
needs beyond this template: its own small `test-registry/` committed
alongside the code, rather than policy-forge's real demo registry checked out
from a second repository -- use whichever matches where your org's actual
compiled policy lives.
