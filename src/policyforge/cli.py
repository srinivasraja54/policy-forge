"""policyctl -- the command line surface.

`policyctl check` is the load-bearing command: it is what runs as a required
status check on every pull request. It needs no API key, no network and no
service, which is precisely why prevention is affordable where nightly
detection-and-nag is not.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .detect import RepoTarget, evaluate_artifact
from .detect import file_pattern
from .detect.base import Outcome, RuleResult
from .ledger import Ledger
from .registry import Registry, RegistryError
from .schema import Artifact, Kind, Severity, Status

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Compile org signals into executable policy, and enforce it deterministically.",
)
console = Console()

DEFAULT_REGISTRY = Path("registry")
DEFAULT_LEDGER = Path("ledger.sqlite")

_MARK = {
    Outcome.CLEAR: "[green]clear[/green]",
    Outcome.VIOLATING: "[red]violating[/red]",
    Outcome.ERROR: "[yellow]error[/yellow]",
}


def _load_dotenv() -> None:
    """Populate the environment from `.env`, if present.

    Only called from the `compile` command's real-model path -- `check` and
    `validate` must keep working with no key, no network and no service, so
    they never touch this. Soft dependency: `python-dotenv` ships with the
    `compile` extra, but a shell that already exports ANTHROPIC_API_KEY works
    with or without it, so a missing package is silent, not an error.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)


def _load(registry: Path) -> Registry:
    try:
        return Registry.load(registry)
    except RegistryError as exc:
        console.print(f"[red]registry error[/red]\n{exc}")
        raise typer.Exit(code=2)


def _targets(reg: Registry, only: list[str] | None, root: Path) -> list[RepoTarget]:
    targets = []
    for repo in reg.catalog.repos:
        if only and repo.name not in only:
            continue
        if not repo.path:
            console.print(f"[yellow]skip[/yellow] {repo.name}: no local path in catalog")
            continue
        targets.append(RepoTarget(name=repo.name, path=root / repo.path))
    return targets


@app.command()
def validate(
    registry: Path = typer.Option(DEFAULT_REGISTRY, "--registry", "-r"),
) -> None:
    """Validate every artifact in the registry. Run this in the registry repo's CI."""
    reg = _load(registry)

    table = Table(title=f"registry: {registry}", header_style="bold")
    for col in ("id", "kind", "status", "severity", "rules", "window / deadline"):
        table.add_column(col)

    now = datetime.now(timezone.utc)
    for a in sorted(reg.artifacts, key=lambda x: (x.kind, x.id)):
        when = ""
        if a.kind is Kind.EVENT and a.window:
            expired = " [dim](expired)[/dim]" if a.is_expired(now) else ""
            when = f"{a.window.start:%Y-%m-%d %H:%M}Z -> {a.window.end:%H:%M}Z{expired}"
        elif a.deadline:
            days = (a.deadline - now.date()).days
            when = f"{a.deadline} ([{'red' if days < 30 else 'dim'}]{days}d[/])"
        table.add_row(a.id, str(a.kind), str(a.status), str(a.severity),
                      str(len(a.rules)), when)

    console.print(table)
    console.print(
        f"[green]OK[/green] {len(reg.artifacts)} artifact(s) valid, "
        f"{len(reg.catalog.repos)} repo(s) in catalog"
    )


@app.command()
def check(
    registry: Path = typer.Option(DEFAULT_REGISTRY, "--registry", "-r"),
    ledger_path: Path = typer.Option(DEFAULT_LEDGER, "--ledger", "-l"),
    repo: list[str] = typer.Option(None, "--repo", help="Limit to named repos."),
    root: Path = typer.Option(Path("."), "--root", help="Base path for catalog paths."),
    record: bool = typer.Option(True, "--record/--no-record", help="Write to the ledger."),
    fail_on_blocking: bool = typer.Option(
        True, "--fail-on-blocking/--no-fail", help="Exit 1 on a blocking violation."
    ),
) -> None:
    """Evaluate active artifacts against repos. No model calls, no network."""
    reg = _load(registry)
    targets = _targets(reg, repo, root)
    if not targets:
        console.print("[yellow]no targets to check[/yellow]")
        raise typer.Exit(code=0)

    now = datetime.now(timezone.utc)
    enforceable = reg.enforceable(now)
    if not enforceable:
        console.print("[dim]no enforceable artifacts (none active, or all expired)[/dim]")
        raise typer.Exit(code=0)

    led = Ledger(ledger_path) if record else None
    run_id = led.start_run(registry_ref=str(registry), note="policyctl check") if led else None

    # Which search backend ran is part of the result, not an implementation
    # detail: the two are required to agree, and a silent switch should still be
    # visible in a CI log if they ever diverge.
    engine = file_pattern.backend()
    console.print(
        f"[dim]{len(enforceable)} artifact(s) x {len(targets)} repo(s) "
        f"· search backend: {engine}"
        + ("  [yellow](install ripgrep for large repos)[/yellow]"
           if engine == "python" else "")
        + "[/dim]"
    )

    all_results: list[RuleResult] = []
    blocking_hits: list[tuple[Artifact, RuleResult]] = []

    for art in enforceable:
        console.print(f"\n[bold]{art.id}[/bold] [dim]{art.kind}/{art.severity}[/dim]")
        for target in targets:
            for res in evaluate_artifact(art, target, now):
                all_results.append(res)
                if res.outcome is Outcome.VIOLATING and art.is_blocking(now):
                    blocking_hits.append((art, res))

                detail = f"n={res.count}" if res.outcome is Outcome.VIOLATING else ""
                console.print(
                    f"  {_MARK[res.outcome]:<22} {res.repo:<16} {res.rule_id:<20} "
                    f"{detail} {res.error or ''}"
                )
                for f in res.findings:
                    loc = f"{f.file}:{f.line}" if f.line else f.file
                    console.print(f"      [dim]{loc}[/dim]  {f.excerpt.strip()[:80]}")
                    if f.detail:
                        console.print(f"        [cyan]{f.detail}[/cyan]")

    if led and run_id:
        led.record(run_id, all_results)
        led.finish_run(run_id)

    violating = sum(1 for r in all_results if r.outcome is Outcome.VIOLATING)
    errored = sum(1 for r in all_results if r.outcome is Outcome.ERROR)
    console.print(
        f"\n{len(all_results)} check(s): [red]{violating} violating[/red], "
        f"[yellow]{errored} error[/yellow], "
        f"[green]{len(all_results) - violating - errored} clear[/green]"
        + (f"  [dim](run {run_id})[/dim]" if run_id else "")
    )

    if errored:
        # A detector that could not run is not a pass. Surface it as a distinct
        # failure so a broken rule never reads as compliance.
        console.print("[yellow]detector errors present -- results are incomplete[/yellow]")

    if blocking_hits and fail_on_blocking:
        console.print(
            f"\n[red bold]BLOCKING[/red bold]: {len(blocking_hits)} violation(s) of "
            "artifacts past their deadline"
        )
        raise typer.Exit(code=1)


@app.command()
def report(
    registry: Path = typer.Option(DEFAULT_REGISTRY, "--registry", "-r"),
    ledger_path: Path = typer.Option(DEFAULT_LEDGER, "--ledger", "-l"),
) -> None:
    """Burndown: per-artifact compliance, from the ledger. Zero model calls."""
    reg = _load(registry)
    led = Ledger(ledger_path)
    rows = led.latest_status()
    if not rows:
        console.print("[yellow]ledger is empty -- run `policyctl check` first[/yellow]")
        raise typer.Exit(code=0)

    now = datetime.now(timezone.utc)
    table = Table(title="compliance burndown", header_style="bold")
    for col in ("artifact", "clear", "violating", "coverage", "due"):
        table.add_column(col)

    by_artifact: dict[str, list] = {}
    for r in rows:
        by_artifact.setdefault(r["artifact_id"], []).append(r)

    for aid, checks in sorted(by_artifact.items()):
        art = reg.by_id(aid)
        repos = {c["repo"] for c in checks}
        bad = {c["repo"] for c in checks if c["outcome"] == "violating"}
        good = len(repos) - len(bad)
        pct = round(100 * good / len(repos)) if repos else 0
        due = ""
        if art and art.deadline:
            d = (art.deadline - now.date()).days
            due = f"{art.deadline} ({d}d)"
        elif art and art.window:
            due = f"{art.window.start:%Y-%m-%d %H:%M}Z"
        colour = "green" if pct == 100 else ("yellow" if pct >= 50 else "red")
        table.add_row(aid, str(good), str(len(bad)),
                      f"[{colour}]{pct}%[/{colour}]", due)

    console.print(table)


@app.command()
def brief(
    team: str = typer.Argument(..., help="Team name as it appears in catalog.yaml."),
    registry: Path = typer.Option(DEFAULT_REGISTRY, "--registry", "-r"),
    ledger_path: Path = typer.Option(DEFAULT_LEDGER, "--ledger", "-l"),
) -> None:
    """One morning digest for a team, instead of N notifications."""
    reg = _load(registry)
    led = Ledger(ledger_path)

    repos = {r.name for r in reg.catalog.repos if r.team == team}
    if not repos:
        console.print(f"[yellow]no repos for team '{team}'[/yellow]")
        console.print(f"known teams: {', '.join(sorted(reg.catalog.teams())) or '(none)'}")
        raise typer.Exit(code=1)

    now = datetime.now(timezone.utc)
    rows = [r for r in led.latest_status()
            if r["repo"] in repos and r["outcome"] == "violating"]

    console.print(f"\n[bold]{team}[/bold] — {now:%Y-%m-%d}")
    console.print(f"[dim]{len(repos)} repo(s): {', '.join(sorted(repos))}[/dim]\n")

    if not rows:
        console.print("[green]No open obligations. Nothing needs your attention.[/green]")
        return

    for aid in sorted({r["artifact_id"] for r in rows}):
        art = reg.by_id(aid)
        if art is None:
            continue
        hits = [r for r in rows if r["artifact_id"] == aid]
        if art.kind is Kind.EVENT and art.window:
            hours = (art.window.start - now).total_seconds() / 3600
            when = f"window opens in {hours:.0f}h ({art.window.start:%a %d %b %H:%M}Z)"
        elif art.deadline:
            d = (art.deadline - now.date()).days
            when = f"deadline {art.deadline} ({d}d)"
        else:
            when = "no deadline"
        flag = "[red]BLOCKING[/red] " if art.is_blocking(now) else ""
        console.print(f"  {flag}[bold]{art.title}[/bold]")
        console.print(f"    [dim]{when} · {aid}[/dim]")
        for h in hits:
            console.print(f"    - {h['repo']}: {h['finding_count']} finding(s) "
                          f"[dim]({h['rule_id']})[/dim]")
        console.print()


@app.command()
def compile(
    source_path: Path = typer.Option(Path("fixtures/slack"), "--source", "-s",
                                     help="Directory of fixture messages."),
    registry: Path = typer.Option(DEFAULT_REGISTRY, "--registry", "-r"),
    ledger_path: Path = typer.Option(DEFAULT_LEDGER, "--ledger", "-l"),
    since_days: int = typer.Option(1, "--since-days", help="Look-back window."),
    stub: bool = typer.Option(False, "--stub", help="Use canned responses; no API key."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not write artifacts."),
    limit: int = typer.Option(None, "--limit", help="Stop after N messages."),
    min_confidence: float = typer.Option(0.4, "--min-confidence"),
) -> None:
    """Compile org signals into proposed artifacts.

    Everything written lands as `status: proposed`. Promoting it to `active` is a
    human decision made in the registry pull request -- that review is the only
    gate between model output and action on someone's repository.
    """
    from .compiler import Engine, Pipeline
    from .sources.fixture import FixtureSource

    if stub:
        from .compiler.stub import StubClient

        client = StubClient()
        console.print("[dim]using stubbed model responses -- no API calls[/dim]")
    else:
        _load_dotenv()
        from .compiler import AnthropicClient

        try:
            client = AnthropicClient()
        except Exception as exc:
            console.print(f"[red]cannot reach the model:[/red] {exc}")
            console.print("[dim]try --stub to run the pipeline offline[/dim]")
            raise typer.Exit(code=2)

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    pipeline = Pipeline(
        engine=Engine(client=client),
        registry_root=registry,
        ledger=Ledger(ledger_path),
        min_confidence=min_confidence,
    )

    console.print(f"[dim]reading {source_path} since {since:%Y-%m-%d %H:%M}Z[/dim]")
    report = pipeline.run(FixtureSource(source_path), since,
                          dry_run=dry_run, limit=limit)

    table = Table(title="compilation", header_style="bold")
    for col in ("ref", "disposition", "artifact", "why"):
        table.add_column(col, overflow="fold")

    colour = {"compiled": "green", "noise": "dim", "duplicate": "dim",
              "amendment": "cyan", "rejected": "yellow", "error": "red"}
    for o in report.outcomes:
        table.add_row(
            o.ref.split("/")[-1][-8:],
            f"[{colour.get(o.disposition, 'white')}]{o.disposition}[/]",
            o.artifact_id or "",
            o.reason[:90],
        )
    console.print(table)

    u = pipeline.engine.usage
    console.print(
        f"\n{len(report.outcomes)} message(s): "
        f"[green]{report.count('compiled')} compiled[/green], "
        f"[cyan]{report.count('amendment')} amendment[/cyan], "
        f"[dim]{report.count('noise')} noise[/dim], "
        f"[dim]{report.count('duplicate')} duplicate[/dim], "
        f"[yellow]{report.count('rejected')} rejected[/yellow], "
        f"[red]{report.count('error')} error[/red]"
    )
    console.print(
        f"[dim]{u.triage_calls} triage call(s), {u.compile_calls} compile call(s) · "
        f"in {u.input_tokens} / out {u.output_tokens} tok · "
        f"cache read {u.cache_read} · ~${u.estimated_cost_usd():.4f}[/dim]"
    )
    if report.written:
        console.print(f"\nwrote {len(report.written)} artifact(s) as "
                      f"[bold]status: proposed[/bold]:")
        for path in report.written:
            console.print(f"  {path}")
        console.print("\n[dim]review them, then set status to 'active' to enforce[/dim]")
    if report.amended:
        # Said separately from the written set, and more loudly. A new artifact
        # is inert until someone promotes it; an amended one may already be
        # active, so merging this diff changes enforcement immediately.
        console.print(f"\n[cyan]amended {len(report.amended)} existing artifact(s)"
                      f"[/cyan]:")
        for path in report.amended:
            console.print(f"  {path}")
        console.print("\n[dim]read the diff -- an amendment to an active artifact "
                      "takes effect on merge[/dim]")
    if dry_run:
        console.print("[yellow]dry run -- nothing written[/yellow]")


if __name__ == "__main__":
    app()
