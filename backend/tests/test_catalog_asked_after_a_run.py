""""Fetch the catalogue now", asked in the moment a run is done with Plex but still prepares the search (the status
says "done" already): a new run waits behind it, instead of nothing happening. While a run still reads Plex, or one
waits, a second click starts nothing."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import Account, Job, SyncRun
from app.services import catalog

from .fakeplex import FakePlex
from .plex_world import connect


def _running_job() -> None:
    with SessionLocal() as db:
        db.add(Job(kind=catalog.FULL, ref=0, payload={}, state="running", created_at=clock.now()))
        db.commit()


def test_asked_while_the_last_run_only_prepares_the_search(client: TestClient, operator: Account,
                                                           fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    _running_job()
    status = client.get("/api/v1/catalog/status").json()
    assert status["running"] is None and status["queued"] is None, "the status says done"
    answer = client.post("/api/v1/catalog/sync")
    assert answer.status_code == 202 and answer.json() == {"started": True}
    assert client.get("/api/v1/catalog/status").json()["queued"] == "full"
    # A second click starts nothing more.
    assert client.post("/api/v1/catalog/sync").json() == {"started": False}


def test_asked_while_a_run_reads_plex_starts_nothing(client: TestClient, operator: Account,
                                                     fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    _running_job()
    with SessionLocal() as db:
        db.add(SyncRun(kind="full", reason="manual", state="running", started_at=clock.now()))
        db.commit()
    assert client.post("/api/v1/catalog/sync").json() == {"started": False}
    with SessionLocal() as db:
        assert db.scalar(select(Job.id).where(Job.kind == catalog.FULL, Job.state == "queued")) is None
