from datetime import datetime, timedelta

from app.prices import NY
from app.scheduler import due_jobs

MON_10 = datetime(2026, 10, 5, 10, 0, tzinfo=NY)    # Monday


def test_first_tick_runs_every_job_that_is_in_its_time_slot():
    assert set(due_jobs(MON_10, {}, holiday=False)) == {"live", "dividends", "house", "sec13f", "senate", "reference"}


def test_live_every_five_minutes_in_market_hours_only():
    last = {"live": MON_10 - timedelta(minutes=4)}
    assert "live" not in due_jobs(MON_10, last, False)
    assert "live" in due_jobs(MON_10, {"live": MON_10 - timedelta(minutes=5)}, False)
    assert "live" not in due_jobs(MON_10.replace(hour=9, minute=29), {}, False)
    assert "live" not in due_jobs(MON_10.replace(hour=16, minute=5), {}, False)
    assert "live" not in due_jobs(MON_10 + timedelta(days=5), {}, False)    # Saturday
    assert "live" not in due_jobs(MON_10, {}, holiday=True)


def test_closes_after_1630_once_per_day():
    t = MON_10.replace(hour=16, minute=31)
    assert "closes" in due_jobs(t, {"closes": t - timedelta(days=1)}, False)
    assert "closes" not in due_jobs(t, {"closes": t.replace(hour=16, minute=30)}, False)
    assert "closes" not in due_jobs(MON_10, {}, False)


def test_intervals():
    last = {"house": MON_10 - timedelta(minutes=59), "senate": MON_10 - timedelta(hours=6)}
    jobs = due_jobs(MON_10, last, False)
    assert "house" not in jobs and "senate" in jobs


def test_weekly_full_prices_on_sunday_morning():
    sun = datetime(2026, 10, 11, 3, 5, tzinfo=NY)
    assert "full_prices" in due_jobs(sun, {}, False)
    assert "full_prices" not in due_jobs(sun, {"full_prices": sun - timedelta(hours=1)}, False)


def test_failed_job_retries_after_fifteen_minutes_within_its_slot():
    t = MON_10.replace(hour=16, minute=31)
    failed = {"closes": t}
    last = {"closes": t - timedelta(days=1)}
    assert "closes" not in due_jobs(t.replace(minute=40), last, False, failed)
    assert "closes" in due_jobs(t.replace(minute=46), last, False, failed)
    assert "closes" not in due_jobs(MON_10, last, False, {"closes": MON_10 - timedelta(hours=1)})   # slot not open


# ---------- final review: backfill per step (item 3) ----------

import json

import pytest

from app import db, pipeline, scheduler
from app.config import Config
from app.pipeline import Ctx


@pytest.fixture
def ctx(tmp_path):
    cfg = Config("a@b.co", "", False, False, 8765, tmp_path)
    return Ctx(cfg, db.connect(tmp_path / "t.db"), tmp_path, now=lambda: MON_10)


def test_runner_backfill_continues_after_a_failing_step(ctx, monkeypatch):
    calls = []

    def ok(name):
        return lambda *a, **k: calls.append(name) or {}

    def senate_down(c):
        calls.append("senate")
        raise RuntimeError("senate site down")
    for name in ("refresh_reference", "sync_house", "resolve_congress_tickers", "sync_13f_datasets", "sync_13f_new",
                 "sync_prices", "build_boards"):
        monkeypatch.setattr(pipeline, name, ok(name))
    monkeypatch.setattr(pipeline, "sync_senate", senate_down)
    monkeypatch.setattr(pipeline, "ensure_securities", lambda c, t, **k: calls.append("ensure_securities") or {})
    monkeypatch.setattr(scheduler, "due_jobs", lambda *a, **k: [])
    scheduler.Runner(ctx).tick()
    assert calls == ["refresh_reference", "sync_house", "senate", "resolve_congress_tickers", "sync_13f_datasets",
                     "sync_13f_new", "ensure_securities", "sync_prices", "build_boards"]
    assert db.get_meta(ctx.conn, "backfill_done")
    st = db.source_status(ctx.conn)
    assert st["senate"]["last_success"] is None and "senate site down" in st["senate"]["last_error"]
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE kind='backfill-step-failed' AND ref='senate' "
                            "AND resolved=0").fetchone()[0] == 1
    assert st["backfill"]["last_success"] is not None


def test_regular_job_success_resolves_the_failed_first_run_step(ctx):
    """A failed first-run step's issue says its regular job will fill it in; once that job succeeds, it is resolved."""
    from app import issues
    with db.transaction(ctx.conn):
        for step in ("senate", "13F data sets", "13F new filings", "prices"):
            issues.record(ctx.conn, "backfill", "backfill-step-failed", step, f"first-run step '{step}' failed")
    r = scheduler.Runner(ctx)

    def still_open():
        return {x[0] for x in ctx.conn.execute("SELECT ref FROM issues WHERE source='backfill' AND resolved=0")}

    assert r._run("house", lambda c: {}) and r._run("senate", lambda c: {}) and r._run("sec13f", lambda c: {})
    assert r._run("live", lambda c: {})                  # a prices job, but not one that reads full history
    assert still_open() == {"prices"}
    assert not r._run("closes", lambda c: 1 / 0)         # a failed run fills in nothing
    assert still_open() == {"prices"}
    assert r._run("closes", lambda c: {})
    assert still_open() == set()


# ---------- final review: live updates on their own thread (item 4) ----------

import threading
import time
from dataclasses import replace


def _wait_for(cond, timeout=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def test_live_runner_keeps_running_while_the_main_runner_is_busy(ctx, monkeypatch):
    clock = {"now": MON_10}
    ctx = replace(ctx, now=lambda: clock["now"])
    with db.transaction(ctx.conn):
        db.set_meta(ctx.conn, "backfill_done", "x")
    busy, release = threading.Event(), threading.Event()

    def slow_house(c):
        busy.set()
        release.wait(5)
        return {}
    lives, boards, threads, kw_seen = [], [], set(), []
    monkeypatch.setattr(scheduler, "JOBS", {"house": slow_house})
    monkeypatch.setattr(pipeline, "sync_live", lambda c: lives.append(clock["now"]) or threads.add(id(c.conn)) or {})
    monkeypatch.setattr(pipeline, "build_boards", lambda c, **kw: boards.append(clock["now"]) or kw_seen.append(kw) or {})
    main = scheduler.Runner(ctx)
    live = scheduler.LiveRunner(ctx.fork(), poll=0.01)
    try:
        main.start()
        assert busy.wait(1)
        live.start()
        assert _wait_for(lambda: len(lives) == 1)
        clock["now"] = MON_10 + timedelta(minutes=4)
        time.sleep(0.1)
        assert len(lives) == 1                                   # not due yet
        clock["now"] = MON_10 + timedelta(minutes=5)
        assert _wait_for(lambda: len(lives) == 2)
        assert _wait_for(lambda: len(boards) == 2)              # each live run is followed by a board rebuild
        assert kw_seen[:2] == [{"live": True}] * 2              # which reads the 13F stats, never computes them
        assert not release.is_set() and main.is_alive()          # the main runner is still inside its long job
        assert threads == {id(live.ctx.conn)} and live.ctx.conn is not ctx.conn
    finally:
        live.stop.set()
        main.stop.set()
        release.set()
        live.join(2)
        main.join(2)
    assert lives == [MON_10, MON_10 + timedelta(minutes=5)]      # the main runner never ran "live" itself


def test_live_runner_waits_for_market_hours_and_skips_boards_before_backfill(ctx, monkeypatch):
    clock = {"now": MON_10.replace(hour=9, minute=0)}
    ctx = replace(ctx, now=lambda: clock["now"])
    lives, boards = [], []
    monkeypatch.setattr(pipeline, "sync_live", lambda c: lives.append(1) or {})
    monkeypatch.setattr(pipeline, "build_boards", lambda c, **kw: boards.append(1) or {})
    live = scheduler.LiveRunner(ctx)
    live.tick()
    assert lives == []
    clock["now"] = MON_10
    live.tick()
    assert lives == [1] and boards == []                         # no boards until the first run has finished
    assert db.source_status(ctx.conn)["prices"]["last_success"] is not None


def test_build_boards_never_runs_twice_at_once(monkeypatch):
    state = {"active": 0, "max": 0}

    def fake(*a, **k):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        time.sleep(0.05)
        state["active"] -= 1
        return {}
    monkeypatch.setattr(pipeline, "_build_boards", fake)
    ts = [threading.Thread(target=pipeline.build_boards, args=(None,)) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert state["max"] == 1


def test_ctx_fork_opens_its_own_connection_to_the_same_file(ctx):
    other = ctx.fork()
    assert other.conn is not ctx.conn
    with db.transaction(other.conn):
        db.set_meta(other.conn, "k", "v")
    assert db.get_meta(ctx.conn, "k") == "v"
