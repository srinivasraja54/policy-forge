"""The ledger: durable record of what was checked, and what we already did about it.

Two jobs, both essential:

1. Audit history -- every enforcement run appends, so "was repo X compliant with
   policy Y on date Z" is answerable without reconstructing anything.
2. Idempotency -- an action row keyed (artifact_id, rule_id, repo) is what stops
   the nightly job opening the same pull request forty times.

SQLite on purpose: the enforcement path must run in CI with no network and no
service dependency.

What this deliberately does *not* do: remember which Slack messages the
compiler has already seen. That dedupe lives in `Registry.source_refs()`
instead, because the registry (git, checked out fresh every run) is durable
across CI runs in a way this file -- gitignored, local to whatever machine
last ran -- is not. See `compiler/pipeline.py`'s module docstring.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .detect.base import RuleResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    registry_ref TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS checks (
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    artifact_id   TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    repo          TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    finding_count INTEGER NOT NULL DEFAULT 0,
    findings      TEXT,
    error         TEXT,
    checked_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_lookup ON checks(artifact_id, repo, checked_at);
CREATE INDEX IF NOT EXISTS idx_checks_run ON checks(run_id);

-- One row per (artifact, rule, repo) we have acted on. UNIQUE is the idempotency
-- guarantee: a second attempt to open the same PR is rejected by the database,
-- not by application logic that might drift.
CREATE TABLE IF NOT EXISTS actions (
    artifact_id TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    repo        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    url         TEXT,
    state       TEXT NOT NULL DEFAULT 'open',
    reason      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT,
    PRIMARY KEY (artifact_id, rule_id, repo)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Ledger:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- runs -------------------------------------------------------------

    def start_run(self, registry_ref: str | None = None, note: str | None = None) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (run_id, started_at, registry_ref, note) VALUES (?,?,?,?)",
                (run_id, _now(), registry_ref, note),
            )
        return run_id

    def finish_run(self, run_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET finished_at=? WHERE run_id=?", (_now(), run_id))

    # ---- checks -----------------------------------------------------------

    def record(self, run_id: str, results: list[RuleResult]) -> int:
        rows = [
            (
                run_id,
                r.artifact_id,
                r.rule_id,
                r.repo,
                str(r.outcome),
                r.count,
                json.dumps([f.as_dict() for f in r.findings]),
                r.error,
                _now(),
            )
            for r in results
        ]
        with self._conn() as c:
            c.executemany(
                "INSERT INTO checks (run_id, artifact_id, rule_id, repo, outcome,"
                " finding_count, findings, error, checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def latest_status(self) -> list[sqlite3.Row]:
        """Most recent outcome per (artifact, rule, repo) -- the burndown source."""
        with self._conn() as c:
            return c.execute(
                """
                SELECT artifact_id, rule_id, repo, outcome, finding_count, checked_at
                FROM checks c
                WHERE checked_at = (
                    SELECT MAX(checked_at) FROM checks c2
                    WHERE c2.artifact_id = c.artifact_id
                      AND c2.rule_id = c.rule_id
                      AND c2.repo = c.repo
                )
                ORDER BY artifact_id, repo, rule_id
                """
            ).fetchall()

    def history(self, repo: str, artifact_id: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM checks WHERE repo=?"
        args: list = [repo]
        if artifact_id:
            q += " AND artifact_id=?"
            args.append(artifact_id)
        with self._conn() as c:
            return c.execute(q + " ORDER BY checked_at DESC", args).fetchall()

    # ---- actions (idempotency) -------------------------------------------

    def claim_action(
        self, artifact_id: str, rule_id: str, repo: str, kind: str
    ) -> bool:
        """Reserve the right to act. False means we already did -- do nothing.

        Called before opening a PR or issue. The UNIQUE primary key makes this
        safe even if two runs overlap.
        """
        with self._conn() as c:
            try:
                c.execute(
                    "INSERT INTO actions (artifact_id, rule_id, repo, kind, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (artifact_id, rule_id, repo, kind, _now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def attach_url(self, artifact_id: str, rule_id: str, repo: str, url: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE actions SET url=?, updated_at=? WHERE artifact_id=? AND"
                " rule_id=? AND repo=?",
                (url, _now(), artifact_id, rule_id, repo),
            )

    def close_action(
        self, artifact_id: str, rule_id: str, repo: str, state: str, reason: str | None = None
    ) -> None:
        """Close an action. A 'rejected' state with a reason is the feedback signal
        that later gets fed back into the compiler as a negative example."""
        with self._conn() as c:
            c.execute(
                "UPDATE actions SET state=?, reason=?, updated_at=? WHERE artifact_id=?"
                " AND rule_id=? AND repo=?",
                (state, reason, _now(), artifact_id, rule_id, repo),
            )

    def open_actions(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM actions WHERE state='open' ORDER BY created_at"
            ).fetchall()
