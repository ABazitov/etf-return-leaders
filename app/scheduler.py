"""When each job runs (spec §10), and the background thread that runs them."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from app import db, issues, pipeline

log = logging.getLogger("etf.scheduler")
RETRY_AFTER = timedelta(minutes=15)
INTERVALS = {"house": timedelta(hours=1), "sec13f": timedelta(hours=1), "senate": timedelta(hours=6)}
JOB_SOURCE = {"live": "prices", "dividends": "prices", "closes": "prices", "full_prices": "prices",
              "house": "house", "senate": "senate", "sec13f": "sec13f", "reference": "reference"}
STALE_AFTER = {"prices": timedelta(minutes=10), "house": timedelta(hours=2), "sec13f": timedelta(hours=2),
               "senate": timedelta(hours=12), "reference": timedelta(days=2), "boards": timedelta(hours=2)}


def _at_or_after(now: datetime, h: int, m: int) -> bool:
    return (now.hour, now.minute) >= (h, m)


def due_jobs(now: datetime, last: dict[str, datetime | None], holiday: bool,
             failed: dict[str, datetime] | None = None) -> list[str]:
    failed = failed or {}
    weekday = now.weekday() < 5
    out = []
    if weekday and not holiday and _at_or_after(now, 9, 30) and not _at_or_after(now, 16, 5):
        if last.get("live") is None or now - last["live"] >= timedelta(minutes=5):
            out.append("live")
    if weekday and not holiday and _at_or_after(now, 9, 45) and (last.get("dividends") is None or last["dividends"].date() < now.date()):
        out.append("dividends")
    if weekday and _at_or_after(now, 16, 30):
        mark = now.replace(hour=16, minute=30, second=0, microsecond=0)
        if last.get("closes") is None or last["closes"] < mark:
            out.append("closes")
    if now.weekday() == 6 and _at_or_after(now, 3, 0) and (last.get("full_prices") is None or last["full_prices"].date() < now.date()):
        out.append("full_prices")
    for job, every in INTERVALS.items():
        if last.get(job) is None or now - last[job] >= every:
            out.append(job)
    if _at_or_after(now, 6, 0) and (last.get("reference") is None or last["reference"].date() < now.date()):
        out.append("reference")
    # a job that just failed waits RETRY_AFTER; its slot condition above still has to hold to be retried
    return [j for j in out if j not in failed or now - failed[j] >= RETRY_AFTER]


# The first-run (backfill) steps each regular job fills in: when the job succeeds, a failed step's issue is resolved.
# "closes" reads only recent rows of tickers already stored, so only the weekly full read fills in fund managers.
BACKFILL_STEPS_FILLED_BY = {"reference": ("reference", "congress tickers"), "house": ("house",), "senate": ("senate",),
                            "sec13f": ("13F data sets", "13F new filings"), "closes": ("prices",),
                            "full_prices": ("prices", "fund managers"), "boards": ("leaderboards",)}

JOBS = {                                     # run by Runner; "live" runs on its own thread (LiveRunner)
    "dividends": pipeline.sync_dividends_today,
    "closes": lambda c: pipeline.sync_prices(c, full=False),
    "full_prices": lambda c: pipeline.sync_prices(c, full=True),
    "house": pipeline.sync_house,
    "senate": pipeline.sync_senate,
    "sec13f": lambda c: {**pipeline.sync_13f_new(c), **pipeline.sync_13f_datasets(c)},
    "reference": lambda c: {**pipeline.refresh_reference(c), **pipeline.resolve_congress_tickers(c)},
}


class _JobThread(threading.Thread):
    """Each thread owns its context and so its own SQLite connection (WAL lets them read and write side by side)."""

    def __init__(self, ctx: pipeline.Ctx, name: str, poll: float):
        super().__init__(daemon=True, name=name)
        self.ctx = ctx
        self.poll = poll
        self.stop = threading.Event()

    def _run(self, name: str, fn) -> bool:
        source = JOB_SOURCE.get(name, name)
        try:
            counters = fn(self.ctx) or {}
            with db.transaction(self.ctx.conn):
                db.set_source_status(self.ctx.conn, source, True, counters=counters)
                for step in BACKFILL_STEPS_FILLED_BY.get(name, ()):
                    issues.clear_ref(self.ctx.conn, "backfill", step)
            return True
        except Exception as e:   # one failing source must not stop the others (spec §11)
            log.exception("job %s failed", name)
            try:
                with db.transaction(self.ctx.conn):
                    db.set_source_status(self.ctx.conn, source, False, error=f"{type(e).__name__}: {e}")
            except Exception:
                log.exception("could not record failure of job %s", name)
            return False

    def _fail_time(self, name: str) -> datetime | None:
        v = db.get_meta(self.ctx.conn, f"job-fail:{name}")
        return datetime.fromisoformat(v) if v else None

    def tick(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:            # e.g. the database is locked for longer than the busy timeout
                log.exception("%s: scheduler tick failed", self.name)
            self.stop.wait(self.poll)


class Runner(_JobThread):
    def __init__(self, ctx: pipeline.Ctx, poll: float = 20):
        super().__init__(ctx, "jobs", poll)

    def _failed(self) -> dict[str, datetime]:
        return {n: t for n in [*JOBS, "backfill"] if (t := self._fail_time(n)) is not None}

    def _record(self, name: str, ok: bool, now: datetime, last: dict) -> None:
        try:
            with db.transaction(self.ctx.conn):
                if ok:
                    last[name] = now
                    db.set_meta(self.ctx.conn, "job-last", json.dumps({k: v.isoformat() for k, v in last.items()}))
                    self.ctx.conn.execute("DELETE FROM meta WHERE key=?", (f"job-fail:{name}",))
                else:
                    db.set_meta(self.ctx.conn, f"job-fail:{name}", now.isoformat())
        except Exception:
            log.exception("could not record outcome of job %s", name)

    def tick(self) -> None:
        conn = self.ctx.conn
        now = self.ctx.now()
        last = {k: datetime.fromisoformat(v) for k, v in json.loads(db.get_meta(conn, "job-last", "{}")).items()}
        failed = self._failed()
        holiday = bool(db.get_meta(conn, f"holiday:{now.date().isoformat()}"))
        changed = False
        if not db.get_meta(conn, "backfill_done") and (
                "backfill" not in failed or now - failed["backfill"] >= RETRY_AFTER):
            ok = self._run("backfill", pipeline.backfill)
            self._record("backfill", ok, now, last)
        for name in due_jobs(now, last, holiday, failed):
            if name not in JOBS:
                continue
            ok = self._run(name, JOBS[name])
            self._record(name, ok, now, last)
            changed |= ok
        if changed:
            self._run("boards", pipeline.build_boards)


class LiveRunner(_JobThread):
    """Live prices every 5 minutes in market hours, then a board rebuild, even while Runner is in a long job."""

    def __init__(self, ctx: pipeline.Ctx, poll: float = 20):
        super().__init__(ctx, "live", poll)

    def tick(self) -> None:
        conn = self.ctx.conn
        now = self.ctx.now()
        v = db.get_meta(conn, "live-last")
        last = {"live": datetime.fromisoformat(v)} if v else {}
        failed = {"live": t} if (t := self._fail_time("live")) is not None else {}
        holiday = bool(db.get_meta(conn, f"holiday:{now.date().isoformat()}"))
        if "live" not in due_jobs(now, last, holiday, failed):
            return
        ok = self._run("live", lambda c: pipeline.sync_live(c))
        try:
            with db.transaction(conn):
                if ok:
                    db.set_meta(conn, "live-last", now.isoformat())
                    conn.execute("DELETE FROM meta WHERE key='job-fail:live'")
                else:
                    db.set_meta(conn, "job-fail:live", now.isoformat())
        except Exception:
            log.exception("could not record outcome of the live job")
        if ok and db.get_meta(conn, "backfill_done"):       # before that there is nothing to rebuild
            self._run("boards", lambda c: pipeline.build_boards(c, live=True))   # reads the 13F stats, never computes them
