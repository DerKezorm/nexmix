"""Music Assistant's events as nexmix hands them on: a scan of its providers fetches the catalogue anew (its events
for single tracks are held back while it scans), single tracks changed now and then too but not at every one, a
playlist made or deleted there makes a look at its list, a closed websocket makes everything due."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import Job
from app.services import jobs, ma_events
from app.services import sync as server_sync

from .fakema import FakeMA
from .ma_world import connect, events


def _full_jobs(server: int) -> int:
    with SessionLocal() as db:
        return len(list(db.scalars(select(Job.id).where(Job.kind == "catalog_full", Job.ref == server,
                                                        Job.state == "queued"))))


def test_a_scan_in_music_assistant_fetches_the_catalogue_anew(client: TestClient, operator: object,
                                                              fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    fake_ma.add_track("Lumen Vale", "Quiet Engines", 2022, "Neu gescannt", 180, number=3)
    fake_ma.scan()
    done = events(server, fake_ma)
    # Only the end of the scan: no event for the single new track.
    assert done.count("catalogue") == 1
    assert _full_jobs(server) == 1
    jobs.run_pending(lane="catalog")
    from app.models import CatalogTrack
    from app.services import media

    with SessionLocal() as db:
        generation = media.generation_of(db, server)
        assert db.scalar(select(CatalogTrack.id).where(CatalogTrack.server_id == server,
                                                       CatalogTrack.generation == generation,
                                                       CatalogTrack.title == "Neu gescannt")) is not None


def test_single_tracks_changed_ask_for_a_run_now_and_then(client: TestClient, operator: object,
                                                          fake_ma: FakeMA, monkeypatch: object) -> None:
    server = connect(client, fake_ma)
    moment = [1000.0]
    import pytest

    patch = pytest.MonkeyPatch()
    patch.setattr(clock, "monotonic", lambda: moment[0])
    try:
        assert ma_events.handle(server, {"event": "media_item_updated", "object_id": "library://track/2903"}) == \
            "catalogue"
        jobs.run_pending(lane="catalog")
        assert ma_events.handle(server, {"event": "media_item_updated", "object_id": "library://track/2904"}) == ""
        moment[0] += ma_events.TRACKS_EVERY + 1
        assert ma_events.handle(server, {"event": "media_item_added", "object_id": "library://album/1903"}) == \
            "catalogue"
    finally:
        patch.undo()


def test_a_playlist_made_there_makes_a_look_and_a_closed_websocket_everything_due(
        client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    with SessionLocal() as db:
        server_sync.tick(db)
    assert server in server_sync._polled_at
    made = fake_ma.ui_create("Von dort")
    assert "look" in events(server, fake_ma)
    assert server not in server_sync._polled_at
    from .ma_world import sync

    sync()
    from app.models import Playlist

    with SessionLocal() as db:
        assert db.scalar(select(Playlist.name)) == "Von dort"
    assert ma_events.handle(server, {"event": "_closed"}) == "again"
    from app.models import ServerPlaylist

    with SessionLocal() as db:
        assert all(row.due_at is not None for row in db.scalars(select(ServerPlaylist)))
    assert made
