"""Work for the background, kept in a table so that it survives a restart.

The mechanism only: a kind of job is a name and a function (``register``), whatever needs background work adds its
own. A job is claimed in one statement (the database lets exactly one claimer through), tried up to ``ATTEMPTS`` times,
and a job left ``running`` by a stopped server goes back to ``queued`` at the next start. At most one job of a kind
waits for the same ``ref`` (a unique index), so a thing changed ten times in a row is handled once.

One loop runs the jobs of its **lane**, one at a time, the oldest first. A kind that waits for something slow (a
service that answers a request a second, a run through a whole catalogue) gets a lane of its own in ``LANES``, so that
it does not stand in front of everything else and nothing stands in front of it. Every kind that has no lane of its own
runs in the lane ``main``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .. import clock
from ..db import SessionLocal
from ..models import Job

logger = logging.getLogger("nexmix.jobs")

#: Set when the server stops, so that a job that is running can stop with it.
stopping = threading.Event()

ATTEMPTS = 5
#: The kinds of job that have a lane of their own, by the name of the lane. Every other kind runs in the lane ``main``.
LANES: dict[str, tuple[str, ...]] = {}
#: How often the loop looks for work.
POLL_SECONDS = 2

#: What a kind of job does: ``function(db, job)``. Raising ``Retry`` or ``GiveUp`` says how to go on.
Handler = Callable[[Session, Job], None]
HANDLERS: dict[str, Handler] = {}
#: What a kind of job does with the work it leaves behind: ``function(db)`` when a job of the kind is given up (it
#: failed for good) and when the lane of the kind starts (a restart). Somebody who waited for a job that is gone would
#: wait for ever.
SETTLERS: dict[str, Callable[[Session], None]] = {}


class Retry(Exception):
    """A job that failed for a reason another try may cure (the time limit, a crash): it waits to be run again until it
    has used its tries."""


class Interrupted(Exception):
    """The server is stopping: the job is put back as it was (no try used up) and runs at the next start."""


class GiveUp(Exception):
    """A job that failed for a reason it names: it stays failed, whatever tries are left."""


def register(kind: str, handler: Handler, settle: Callable[[Session], None] | None = None) -> None:
    """Teaches the loop a kind of job. The name is at most 16 characters (the column). ``settle`` is called when a job
    of the kind is given up and when its lane starts: it puts right whatever waited for a job that no longer lives."""
    if len(kind) > 16:
        raise ValueError("the name of a kind of job is at most 16 characters")
    HANDLERS[kind] = handler
    if settle is not None:
        SETTLERS[kind] = settle


def settle(db: Session, kinds: Iterable[str]) -> None:
    for kind in kinds:
        settler = SETTLERS.get(kind)
        if settler is None:
            continue
        try:
            settler(db)
        except Exception:
            db.rollback()
            logger.exception("Settling after jobs of kind %s failed", kind)


def enqueue(db: Session, kind: str, ref: int = 0, payload: Any = None) -> None:
    """A job in the caller's transaction; a no-op while one of the same kind already waits for the same ``ref``."""
    db.execute(sqlite_insert(Job).values(kind=kind, ref=ref, payload=payload, state="queued", created_at=clock.now())
               .on_conflict_do_nothing())


def recover(db: Session, lane: str | None = None) -> None:
    """At start: what a stopped server left ``running`` waits again (or is dropped when the same is already waiting). Of
    one lane only, when a lane is given: each lane puts back its own work, so that none of them depends on another one
    having started first."""
    twin = Job.__table__.alias("twin")
    left_running = [Job.state == "running", *_of_lane(lane)]
    waiting_again = ~select(twin.c.id).where(twin.c.state == "queued", twin.c.kind == Job.kind,
                                             twin.c.ref == Job.ref).exists()
    db.execute(update(Job).where(*left_running, waiting_again).values(state="queued"))
    db.execute(update(Job).where(*left_running).values(state="done", finished_at=clock.now()))
    db.commit()


def _of_lane(lane: str | None) -> list[Any]:
    """The conditions on ``Job.kind`` that make a job one of a lane: ``main`` is every kind that has no lane of its own,
    a named lane its kinds, and no lane (a test, a command) is every kind."""
    if lane is None:
        return []
    if lane == "main":
        return [Job.kind.not_in([kind for kinds in LANES.values() for kind in kinds])]
    return [Job.kind.in_(LANES[lane])]


def _claim(db: Session, skip: set[int], lane: str | None = None) -> Job | None:
    """The oldest waiting job of the lane that was not tried in this round, marked running in the same statement."""
    row = db.execute(
        update(Job).where(Job.id == select(Job.id).where(Job.state == "queued", Job.id.not_in(skip), *_of_lane(lane))
                          .order_by(Job.id).limit(1).scalar_subquery())
        .values(state="running", started_at=clock.now(), attempts=Job.attempts + 1).returning(Job.id)
    ).first()
    db.commit()
    return db.get(Job, row[0]) if row else None


def run_pending(limit: int = 1000, lane: str | None = None) -> int:
    """Runs the waiting jobs of a lane (all of them without one), oldest first, until none is left or ``limit`` ran.
    Returns how many ran."""
    ran = 0
    tried: set[int] = set()
    while ran < limit and not stopping.is_set():
        with SessionLocal() as db:
            job = _claim(db, tried, lane)
            if job is None:
                return ran
            tried.add(job.id)
            ran += 1
            try:
                handler = HANDLERS.get(job.kind)
                if handler is None:
                    raise GiveUp("unknown job")
                handler(db, job)
            except Interrupted:
                db.rollback()
                db.execute(update(Job).where(Job.id == job.id).values(state="queued", attempts=Job.attempts - 1))
                db.commit()
                return ran
            except Exception as exc:  # noqa: BLE001 - a job that fails must not stop the others
                db.rollback()
                # A job that is not given up on waits for the next round.
                failed = isinstance(exc, GiveUp) or job.attempts >= ATTEMPTS
                name = str(exc)[:60] if isinstance(exc, Retry | GiveUp) and str(exc) else type(exc).__name__
                logger.warning("Job failed kind=%s ref=%s attempt=%s error=%s", job.kind, job.ref, job.attempts, name)
                db.execute(update(Job).where(Job.id == job.id).values(
                    state="failed" if failed else "queued", error=name, finished_at=clock.now()))
                db.commit()
                if failed:
                    settle(db, (job.kind,))
            else:
                db.execute(update(Job).where(Job.id == job.id).values(state="done", finished_at=clock.now(), error=""))
                db.commit()
    return ran


#: Finished jobs (done or failed): none older than this many days, never more than this many.
KEEP_DAYS = 30
KEEP_FINISHED = 1000


def prune(db: Session, now: datetime | None = None) -> None:
    """Finished jobs go after ``KEEP_DAYS``, and beyond the newest ``KEEP_FINISHED``; a job that waits or runs stays.
    In the caller's transaction."""
    moment = now or clock.now()
    finished = Job.state.in_(("done", "failed"))
    db.execute(delete(Job).where(finished, Job.finished_at < moment - timedelta(days=KEEP_DAYS)))
    oldest_kept = (select(Job.id).where(finished).order_by(Job.id.desc())
                   .offset(KEEP_FINISHED - 1).limit(1).scalar_subquery())
    db.execute(delete(Job).where(finished, Job.id < oldest_kept))


def waiting(db: Session) -> int:
    """How many jobs are queued or running."""
    return len(db.scalars(select(Job.id).where(Job.state.in_(("queued", "running")))).all())


async def run_forever(stop: asyncio.Event, lane: str = "main") -> None:
    """The loop of one lane. Each lane puts back, when it starts, what a stopped server left running in it, whichever
    lane starts first."""
    stopping.clear()
    with SessionLocal() as db:
        recover(db, lane)
        settle(db, LANES.get(lane, ()))
    try:
        while not stop.is_set():
            try:
                await asyncio.to_thread(run_pending, 50, lane)
            except Exception:
                logger.exception("Background run failed")
            try:
                await asyncio.wait_for(stop.wait(), POLL_SECONDS)
            except TimeoutError:
                continue
    finally:
        stopping.set()
