"""Harvest checkpoints are per (project, transcript source) — issue #312.

``skillopt-sleep run`` used to store one harvest cursor per project, whatever
``--source`` the run used. When one project is fed from several sources, every
source after the first only saw sessions newer than the first run's start, so
its older, never-harvested sessions were skipped for good.

Offline: mock backend, no model calls, no network.
Run:  python -m pytest tests/test_harvest_checkpoint_source.py
"""
from __future__ import annotations

import json
import os

from skillopt_sleep.config import load_config
from skillopt_sleep.state import SleepState, harvest_cursor_key

PROJECT = "/work/proj"


def _write_claude_session(transcripts_dir: str, session_id: str, project: str) -> None:
    """Two-turn interactive session, ended a few minutes ago (inside lookback)."""
    from datetime import datetime, timedelta

    folder = os.path.join(transcripts_dir, "projects", "demo")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, session_id + ".jsonl")
    base = datetime.now() - timedelta(minutes=5)
    with open(path, "w", encoding="utf-8") as f:
        for i, (role, text) in enumerate((
            ("user", "Fix the failing date parser test in module B."),
            ("assistant", "Fixed."),
            ("user", "Thanks, now add a regression test."),
            ("assistant", "Added."),
        )):
            timestamp = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            f.write(json.dumps({
                "type": role, "cwd": project, "sessionId": session_id,
                "timestamp": timestamp,
                "message": {"role": role, "content": text},
            }) + "\n")


# ── state level ─────────────────────────────────────────────────────────────

def test_cursors_are_keyed_per_project_and_source(tmp_path):
    state = SleepState.load(str(tmp_path / "state.json"))
    state.set_last_harvest(PROJECT, "2026-01-02T03:00:00", "codex")

    assert state.last_harvest_for(PROJECT, "codex") == "2026-01-02T03:00:00"
    # another source of the same project has no cursor of its own
    assert state.last_harvest_for(PROJECT, "cursor") is None
    # and no project-wide cursor was written for it to inherit
    assert state.last_harvest_for(PROJECT) is None
    assert state.data["last_harvest"] == {harvest_cursor_key(PROJECT, "codex"):
                                          "2026-01-02T03:00:00"}


def test_sources_do_not_share_one_cursor(tmp_path):
    state = SleepState.load(str(tmp_path / "state.json"))
    state.set_last_harvest(PROJECT, "2026-01-02T03:00:00", "codex")
    state.set_last_harvest(PROJECT, "2026-01-05T03:00:00", "cursor")

    assert state.last_harvest_for(PROJECT, "codex") == "2026-01-02T03:00:00"
    assert state.last_harvest_for(PROJECT, "cursor") == "2026-01-05T03:00:00"


def test_legacy_bare_project_cursor_is_still_read_and_kept(tmp_path):
    # Older state files key the cursor by project alone; an upgrade must not
    # re-scan the whole transcript history, so it is read as the fallback.
    state = SleepState.load(str(tmp_path / "state.json"))
    state.set_last_harvest(PROJECT, "2026-01-02T03:00:00")

    assert state.last_harvest_for(PROJECT, "codex") == "2026-01-02T03:00:00"
    assert state.last_harvest_for(PROJECT) == "2026-01-02T03:00:00"

    # A per-source write must not overwrite the legacy cursor: other sources
    # still have only that one to fall back on.
    state.set_last_harvest(PROJECT, "2026-01-05T03:00:00", "codex")
    assert state.last_harvest_for(PROJECT, "cursor") == "2026-01-02T03:00:00"
    assert state.last_harvest_for(PROJECT, "codex") == "2026-01-05T03:00:00"
    assert state.last_harvest_for(PROJECT) == "2026-01-02T03:00:00"


# ── end to end (issue #312 repro) ───────────────────────────────────────────

def _cfg(project: str, claude_home: str, cursor_home: str, source: str):
    return load_config(
        invoked_project=project, projects="all", backend="mock",
        claude_home=claude_home, cursor_home=cursor_home,
        transcript_source=source, lookback_hours=24,
    )


def test_second_source_run_does_not_hide_the_first_sources_sessions(tmp_path):
    from skillopt_sleep.cycle import run_sleep_cycle

    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    claude_home = str(tmp_path / ".claude")
    os.makedirs(claude_home)
    # an empty Cursor store: that run harvests nothing but persists state
    cursor_home = str(tmp_path / ".cursor")
    os.makedirs(os.path.join(cursor_home, "projects"))
    _write_claude_session(os.path.join(claude_home, "projects"), "real-1", proj)
    _write_claude_session(os.path.join(claude_home, "projects"), "real-2", proj)

    claude = _cfg(proj, claude_home, cursor_home, "claude")
    cursor = _cfg(proj, claude_home, cursor_home, "cursor")

    first = run_sleep_cycle(claude, dry_run=True)
    assert first.report.n_sessions == 2

    # stateful run of a DIFFERENT source in the same project: it persists a
    # checkpoint for itself only (#312: this used to move the project cursor)
    other = run_sleep_cycle(cursor, dry_run=False)
    assert other.report.n_sessions == 0

    # the claude sessions were never harvested, so they must still be there
    again = run_sleep_cycle(claude, dry_run=True)
    assert again.report.n_sessions == 2

    state = SleepState.load(claude.state_path)
    assert state.last_harvest_for(proj, "cursor") is not None
    assert state.last_harvest_for(proj, "claude") is None


def test_a_source_still_skips_what_it_already_harvested(tmp_path):
    from skillopt_sleep.cycle import run_sleep_cycle

    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    claude_home = str(tmp_path / ".claude")
    os.makedirs(claude_home)
    cursor_home = str(tmp_path / ".cursor")
    os.makedirs(os.path.join(cursor_home, "projects"))
    _write_claude_session(os.path.join(claude_home, "projects"), "real-1", proj)

    claude = _cfg(proj, claude_home, cursor_home, "claude")
    first = run_sleep_cycle(claude, dry_run=False)
    assert first.report.n_sessions == 1

    # its own cursor advanced: the same session is not harvested twice
    assert run_sleep_cycle(claude, dry_run=True).report.n_sessions == 0
