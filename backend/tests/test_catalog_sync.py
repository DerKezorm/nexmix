"""The catalogue against the stand-in Plex: the full run, the partial run with its overlap and its limit, what is gone,
a restart in the middle, two asks at once, Plex failing in the middle (the old catalogue stays), the clock."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import clock
from app import db as database
from app.db import SessionLocal
from app.models import CatalogAlbum, CatalogArtist, CatalogTag, CatalogTrack, Job, MediaServer, SyncRun, TrackTag
from app.services import backups, catalog, jobs, plex_link, settings_service
from app.services.sources import plex as plex_source

from .conftest import make_account, new_client
from .fakeplex import FakePlex
from .fakeplex.library import ALBUM, BASE, TRACK, Item

RECORDS = Path(__file__).parent / "schema"


def connect(client: TestClient, fake: FakePlex) -> None:
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake.account_token})
    assert answer.status_code == 200, answer.text


def run() -> int:
    return jobs.run_pending(lane="catalog")


def live() -> int:
    with SessionLocal() as db:
        row = plex_link.current(db)
        return row.generation if row else 0


def tracks() -> dict[int, CatalogTrack]:
    with SessionLocal() as db:
        found = db.scalars(select(CatalogTrack).where(CatalogTrack.generation == live())).all()
        for one in found:
            db.expunge(one)
    return {one.id: one for one in found}


def tags_of(track: int) -> set[tuple[str, str]]:
    with SessionLocal() as db:
        rows = db.execute(select(CatalogTag.kind, CatalogTag.name).join(
            TrackTag, (TrackTag.tag_id == CatalogTag.id) & (TrackTag.generation == CatalogTag.generation))
            .where(TrackTag.item_id == track, TrackTag.generation == live())).all()
    return {(kind, name) for kind, name in rows}


def runs() -> list[SyncRun]:
    with SessionLocal() as db:
        found = db.scalars(select(SyncRun).order_by(SyncRun.id)).all()
        for one in found:
            db.expunge(one)
    return list(found)


@pytest.fixture
def synced(client: TestClient, operator: object, fake_plex: FakePlex) -> FakePlex:
    connect(client, fake_plex)
    assert run() == 1
    return fake_plex


# --- The full run ----------------------------------------------------------------------------------------------------


def test_the_full_run_takes_in_the_whole_library(client: TestClient, synced: FakePlex) -> None:
    status = client.get("/api/v1/catalog/status").json()
    assert status["ready"] is True and status["running"] is None and status["queued"] is None
    assert status["counts"] == {"artists": 5, "albums": 11, "tracks": 32, "songs": 16, "moods": 10}
    assert status["last"]["kind"] == "full" and status["last"]["state"] == "done"
    assert status["last"]["progress"] == 1000 and status["last"]["reason"] == "first"
    found = tracks()
    compilation = found[3023]
    assert compilation.artist == "Mira Solenne" and compilation.year == 2022
    # Where the track's own artist is the album's, nothing is kept twice.
    assert found[3001].artist == ""
    assert found[3013].artist == "Mira Solenne feat. Dax Orlo"
    assert found[3001].duration == 200_000 and found[3001].rating_count == 52_000
    assert found[3009].view_count == 40
    with SessionLocal() as db:
        album = db.get(CatalogAlbum, (1, live(), 2001))
        assert album is not None and album.thumb.startswith("/library/metadata/2001/thumb/")
        assert db.get(CatalogArtist, (1, live(), 1001)).title == "The Velvet Harbour"  # type: ignore[union-attr]


def test_lists_come_in_pages_of_five_thousand_and_tags_many_at_once(synced: FakePlex) -> None:
    lists = [params for _method, path, params in synced.library_calls() if path.endswith("/all")]
    pages = [params for params in lists if "mood" not in params and "type" in params]
    assert {params["X-Plex-Container-Size"] for params in pages} == {"5000"}
    bulk = [path for _method, path, _params in synced.library_calls() if path.startswith("/library/metadata/")]
    # Albums, artists and the sample of the sonic analysis: three requests, not one per entry.
    assert len(bulk) == 3
    assert any(path.count(",") == 10 for path in bulk)
    moods = [params for params in lists if "mood" in params]
    assert moods and all(params["type"] == "10" and "excludeElements" in params for params in moods)


def test_tags_of_tracks_albums_and_artists(synced: FakePlex) -> None:
    assert tags_of(3001) == {("mood", "Energetic"), ("genre", "Pop")}
    assert tags_of(3013) == {("mood", "Warm"), ("mood", "Romantic"), ("genre", "Pop")}
    with SessionLocal() as db:
        def of(model: type, item: int) -> set[tuple[str, str]]:
            rows = db.execute(select(CatalogTag.kind, CatalogTag.name).join(
                model, (model.tag_id == CatalogTag.id) & (model.generation == CatalogTag.generation))
                .where(model.item_id == item, model.generation == live())).all()
            return {(kind, name) for kind, name in rows}

        from app.models import AlbumTag, ArtistTag

        assert of(AlbumTag, 2001) == {("mood", "Melancholy"), ("style", "Dream Pop"), ("genre", "Pop")}
        assert of(AlbumTag, 2003) == {("genre", "Pop"), ("genre", "Rock")}
        assert of(ArtistTag, 1001) == {("mood", "Brooding"), ("mood", "Atmospheric"), ("style", "Dream Pop"),
                                       ("genre", "Rock")}


def test_songs_versions_and_original_years(synced: FakePlex) -> None:
    found = tracks()
    shown = {one.id for one in found.values() if one.shown}
    assert shown == {3001, 3002, 3003, 3004, 3005, 3006, 3011, 3013, 3014, 3017, 3018, 3020, 3021, 3022, 3026, 3030}
    northern = [one for one in found.values() if one.song_key == found[3001].song_key]
    # The album, the remaster, the best-of, the karaoke and the live version: one song, shown from 2001.
    assert {one.id for one in northern} == {3001, 3007, 3009, 3012, 3031}
    assert {one.original_year for one in northern} == {2001}
    assert found[3012].variant and found[3031].variant and not found[3009].variant
    assert found[3020].song_key != found[3001].song_key
    # The intro of a song is no version of it; part 1 and part 2 are two pieces; the intros of two albums too.
    assert found[3002].song_key != found[3003].song_key
    assert found[3004].song_key != found[3005].song_key
    assert found[3006].song_key != found[3011].song_key
    # The compilation track and the instrumental album join Copper Sky; the edit of Fever Glass is not shown.
    assert found[3023].song_key == found[3019].song_key == found[3013].song_key
    assert found[3013].original_year == 2016 and found[3023].year == 2022
    assert found[3016].variant and found[3016].song_key == found[3017].song_key
    # Paper Planes: the compilation, the extended mix, the demo, the deluxe edition: all from 1998.
    paper = {one.id for one in found.values() if one.song_key == found[3026].song_key}
    assert paper == {3024, 3025, 3026, 3027, 3028, 3029}
    assert found[3024].original_year == 1998


def newest(fake: FakePlex, kind: int) -> int:
    return max(max(item.added, item.updated or 0) for item in fake.items.values() if item.kind == kind)


def test_marks_are_plex_time_never_ours(synced: FakePlex, monkeypatch: pytest.MonkeyPatch) -> None:
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None
        # The newest time seen per type, and Plex's clock when the run began (the ``Date`` of its first answer).
        assert row.marks == {"8": newest(synced, 8), "9": newest(synced, 9), "10": newest(synced, 10),
                             "clock": synced.now}
        assert row.marks["10"] == BASE - 3600


def test_the_search_never_sees_half_a_catalogue(client: TestClient, synced: FakePlex) -> None:
    before = client.get("/api/v1/catalog/status").json()["counts"]
    seen: list[dict[str, int]] = []

    def look(count: int, path: str) -> None:
        if path.startswith("/library/metadata/"):
            with SessionLocal() as db:
                seen.append(catalog.counts(db, plex_link.current(db).generation))  # type: ignore[union-attr]

    synced.add(Item(3099, TRACK, "Lantern Song", 2001))
    synced.hook = look
    assert client.post("/api/v1/catalog/sync").json() == {"started": True}
    run()
    assert seen and all(one == before for one in seen)
    after = client.get("/api/v1/catalog/status").json()["counts"]
    assert after["tracks"] == before["tracks"] + 1
    # The new generation, and the one before for whoever still reads it.
    with SessionLocal() as db:
        assert {row[0] for row in db.execute(select(CatalogTrack.generation).distinct())} == {live() - 1, live()}


def test_a_reader_in_the_middle_of_the_switch_sees_one_catalogue(client: TestClient, synced: FakePlex,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """The state of the catalogue is read in several queries: a full run that switches over between the first and the
    last of them must not change what the reader sees."""
    before = client.get("/api/v1/catalog/status").json()
    synced.add(Item(3099, TRACK, "Lantern Song", 2001))
    client.post("/api/v1/catalog/sync")
    counts = catalog.counts
    switched: list[int] = []

    def in_between(db: Session, generation: int, server_id: int | None = None) -> dict[str, int]:
        # The reader has read which generation is live; now the full run goes through, start to end.
        if not switched:
            switched.append(run())
        return counts(db, generation, server_id)

    monkeypatch.setattr(catalog, "counts", in_between)
    seen = client.get("/api/v1/catalog/status").json()
    assert switched == [1]
    assert seen["counts"] == before["counts"]
    monkeypatch.setattr(catalog, "counts", counts)
    assert client.get("/api/v1/catalog/status").json()["counts"]["tracks"] == before["counts"]["tracks"] + 1


def test_the_old_generation_stays_until_the_next_full_run_begins(client: TestClient, synced: FakePlex) -> None:
    first = live()
    reader = SessionLocal()
    try:
        pinned = catalog.pin(reader)
        assert pinned == first
        client.post("/api/v1/catalog/sync")
        run()
        second = live()
        assert second != first
        # The reader still reads its generation, whole.
        assert catalog.counts(reader, pinned)["tracks"] == 32
    finally:
        reader.close()
    with SessionLocal() as db:
        assert {row[0] for row in db.execute(select(CatalogTrack.generation).distinct())} == {first, second}
    # A partial run leaves both as they are; the next full run makes room first.
    partial()
    with SessionLocal() as db:
        assert {row[0] for row in db.execute(select(CatalogTrack.generation).distinct())} == {first, second}
    seen: list[set[int]] = []

    def look(count: int, path: str) -> None:
        if path.startswith("/library/metadata/") and not seen:
            with SessionLocal() as db:
                seen.append({row[0] for row in db.execute(select(CatalogTrack.generation).distinct())})

    synced.hook = look
    client.post("/api/v1/catalog/sync")
    run()
    assert seen == [{second, second + 1}]
    with SessionLocal() as db:
        assert {row[0] for row in db.execute(select(CatalogTrack.generation).distinct())} == {second, second + 1}


def test_a_failed_run_after_a_switch_leaves_the_old_generation_for_its_readers(client: TestClient,
                                                                              synced: FakePlex) -> None:
    first = live()
    client.post("/api/v1/catalog/sync")
    run()
    second = live()
    synced.fail_after, synced.fail_mode = synced.library_requests + 1, "503"
    partial()
    assert runs()[-1].state == "failed"
    with SessionLocal() as db:
        assert {row[0] for row in db.execute(select(CatalogTrack.generation).distinct())} == {first, second}


# --- Plex fails, a restart, two asks -----------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "code", "at", "step"), [
    ("503", "plex_busy", 2, "tracks"), ("timeout", "plex_timeout", 3, "album_tags"),
    ("drop", "plex_unreachable", 7, "track_moods"), ("401", "plex_refused", 12, "analysis"),
])
def test_when_plex_fails_in_the_middle_the_old_catalogue_stays(client: TestClient, synced: FakePlex, mode: str,
                                                              code: str, at: int, step: str) -> None:
    before = client.get("/api/v1/catalog/status").json()
    generation = live()
    synced.fail_after, synced.fail_mode = synced.library_requests + at, mode
    client.post("/api/v1/catalog/sync")
    run()
    after = client.get("/api/v1/catalog/status").json()
    assert after["counts"] == before["counts"] and after["ready"] is True
    assert after["last"]["state"] == "failed" and after["last"]["error"] == code
    assert after["last"]["step"] == step
    assert live() == generation
    with SessionLocal() as db:
        assert {row[0] for row in db.execute(select(CatalogTrack.generation).distinct())} == {generation}
        assert db.scalars(select(Job.state).where(Job.kind == "catalog_full")).all()[-1] == "failed"


def test_the_first_run_failing_leaves_no_catalogue_and_says_why(client: TestClient, operator: object,
                                                                fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    fake_plex.down.add("127.0.0.1")
    run()
    status = client.get("/api/v1/catalog/status").json()
    assert status["ready"] is False and status["last"]["error"] == "plex_unreachable"
    with SessionLocal() as db:
        assert db.query(CatalogTrack).count() == 0


def test_a_server_stopping_in_the_middle_puts_the_job_back_and_it_runs_again(client: TestClient, operator: object,
                                                                             fake_plex: FakePlex) -> None:
    connect(client, fake_plex)

    def stop(count: int, path: str) -> None:
        if count == 8:
            jobs.stopping.set()

    fake_plex.hook = stop
    run()
    with SessionLocal() as db:
        assert db.scalars(select(Job.state).where(Job.kind == "catalog_full")).all() == ["queued"]
        assert db.query(CatalogTrack).count() == 0
    assert [(one.state, one.error) for one in runs()] == [("stopped", "stopped")]
    jobs.stopping.clear()
    fake_plex.hook = None
    run()
    assert client.get("/api/v1/catalog/status").json()["counts"]["tracks"] == 32
    assert [one.state for one in runs()] == ["stopped", "done"]


def test_a_crash_in_the_middle_is_cleaned_up_at_the_next_start(client: TestClient, operator: object,
                                                               fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    with SessionLocal() as db:
        job = db.scalars(select(Job).where(Job.kind == "catalog_full")).one()
        job.state = "running"
        db.add(SyncRun(kind="full", state="running", serial=1, generation=1, started_at=clock.now(), server_id=1))
        # Half a catalogue of the run that died with the server.
        db.add(CatalogTrack(server_id=1, generation=1, id=3001, title="Northern Lights"))
        db.commit()
        jobs.recover(db, catalog.LANE)
        jobs.settle(db, jobs.LANES[catalog.LANE])
        assert db.query(CatalogTrack).count() == 0
        assert db.scalars(select(Job.state)).all() == ["queued"]
    assert [(one.state, one.error) for one in runs()] == [("stopped", "interrupted")]
    run()
    assert client.get("/api/v1/catalog/status").json()["counts"]["tracks"] == 32


def test_two_asks_at_once_start_one_run(client: TestClient, synced: FakePlex) -> None:
    answers = [client.post("/api/v1/catalog/sync").json() for _ in range(2)]
    assert answers == [{"started": True}, {"started": False}]
    with SessionLocal() as db:
        assert db.query(Job).filter_by(kind="catalog_full", state="queued").count() == 1
    started: list[bool] = []
    barrier = threading.Barrier(6)

    def ask() -> None:
        barrier.wait()
        with SessionLocal() as db:
            started.append(catalog.request_full(db, "manual"))

    run()
    threads = [threading.Thread(target=ask) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert started.count(True) <= 1
    with SessionLocal() as db:
        assert db.query(Job).filter_by(kind="catalog_full", state="queued").count() == 1
    assert run() == 1


def test_no_second_run_while_one_is_running(client: TestClient, synced: FakePlex) -> None:
    answers: list[dict[str, bool]] = []

    def ask(count: int, path: str) -> None:
        if count == synced_start + 3 and not answers:
            with new_client(admin) as other:
                answers.append(other.post("/api/v1/catalog/sync").json())

    with SessionLocal() as db:
        from app.models import Account

        admin = db.query(Account).filter_by(name="tester").one()
        db.expunge(admin)
    synced_start = synced.library_requests
    synced.hook = ask
    client.post("/api/v1/catalog/sync")
    assert run() == 1
    assert answers == [{"started": False}]


def test_the_library_changed_in_the_middle_throws_the_run_away(client: TestClient, synced: FakePlex) -> None:
    generation = live()

    def switch(count: int, path: str) -> None:
        if path.startswith("/library/metadata/"):
            with SessionLocal() as db:
                row = plex_link.current(db)
                assert row is not None
                row.serial += 1
                db.commit()

    synced.hook = switch
    client.post("/api/v1/catalog/sync")
    run()
    assert live() == generation
    assert runs()[-1].state == "stopped" and runs()[-1].error == "replaced"
    with SessionLocal() as db:
        assert {row[0] for row in db.execute(select(CatalogTrack.generation).distinct())} == {generation}


# --- The partial run -----------------------------------------------------------------------------------------------


def partial() -> None:
    with SessionLocal() as db:
        assert catalog.request_partial(db) is True
    assert jobs.run_pending(limit=1, lane="catalog") == 1


def test_a_partial_run_takes_in_what_changed_and_what_is_new(client: TestClient, synced: FakePlex) -> None:
    synced.touch(3017, title="Fever Glass (2026 Remaster)", rating=7_500)
    synced.add(Item(3040, TRACK, "Lantern Song", 2005, moods=["Dreamy"], genres=["Pop"]))
    synced.add(Item(2012, ALBUM, "Night Ferries", 1002, year=2026, genres=["Pop"], moods=["Dreamy"]))
    synced.add(Item(3041, TRACK, "Ferry Lights", 2012))
    synced.calls.clear()
    partial()
    found = tracks()
    assert found[3017].title == "Fever Glass (2026 Remaster)" and found[3017].rating_count == 7_500
    assert found[3040].title == "Lantern Song" and ("mood", "Dreamy") in tags_of(3040)
    assert found[3041].year == 2026
    assert found[3040].song_key and found[3040].shown
    last = runs()[-1]
    # Four changes; the minute before the last run began held nothing.
    assert last.kind == "partial" and last.state == "done" and last.changed == 4 and last.removed == 0
    # Only the changes and the counts were asked for: no page of the whole library.
    pages = [params for _method, path, params in synced.library_calls() if path.endswith("/all")
             and params.get("X-Plex-Container-Size") != "0"]
    assert pages and all(any(name.endswith(">>") for name in params) for params in pages)
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None and row.marks["10"] == synced.items[3041].added


def test_the_partial_run_looks_sixty_seconds_back_on_plexs_clock(client: TestClient, synced: FakePlex,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    # Our own clock is far off; it must not matter.
    monkeypatch.setattr(clock, "now", lambda: datetime(2031, 1, 1, tzinfo=UTC))
    synced.calls.clear()
    # When the full run began, Plex's clock said ``began``. Plex wrote this a little before (another thread of Plex,
    # a second late), and it was not there yet to be fetched.
    began = synced.now
    synced.tick(600)
    synced.items[3003].title = "Glasshouse (Single)"
    synced.items[3003].updated = began - 30
    partial()
    counts = [params for _method, path, params in synced.library_calls() if path.endswith("/all")
              and params.get("X-Plex-Container-Size") == "0" and params.get("type") == "10"
              and ("updatedAt>>" in params or "addedAt>>" in params)]
    # New since then (counted against the limit), and the minute before (fetched).
    assert {params.get("updatedAt>>") or params.get("addedAt>>") for params in counts} == {str(began),
                                                                                          str(began - 60)}
    pages = [params for _method, path, params in synced.library_calls() if path.endswith("/all")
             and params.get("type") == "10" and params.get("X-Plex-Container-Size") == "5000"]
    assert {params.get("updatedAt>>") or params.get("addedAt>>") for params in pages} == {str(began - 60)}
    assert tracks()[3003].title == "Glasshouse (Single)"
    # The next run looks from where this one began on Plex's clock, not from ours.
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None and row.marks["clock"] == began + 600


def test_too_many_changes_hand_over_to_a_full_run(client: TestClient, synced: FakePlex,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plex_source, "PARTIAL_LIMIT", 3)
    for key in (3001, 3002, 3003, 3004):
        synced.touch(key, rating=1)
    synced.calls.clear()
    partial()
    # The counts said too many: not one page of changes was fetched, the full run takes over.
    pages = [params for _method, path, params in synced.library_calls() if path.endswith("/all")
             and params.get("X-Plex-Container-Size") != "0"]
    assert pages == []
    with SessionLocal() as db:
        waiting = db.scalars(select(Job).where(Job.kind == "catalog_full", Job.state == "queued")).one()
        assert waiting.payload["reason"] == "many"
    assert runs()[-1].step == "many"
    run()
    assert runs()[-1].kind == "full" and runs()[-1].state == "done"
    assert tracks()[3001].rating_count == 1


def test_what_is_gone_from_plex_goes_from_the_catalogue(client: TestClient, synced: FakePlex) -> None:
    synced.remove(3018)
    synced.remove(2009)
    partial()
    found = tracks()
    assert 3018 not in found and 3019 not in found
    with SessionLocal() as db:
        assert db.get(CatalogAlbum, (1, live(), 2009)) is None
        assert db.scalars(select(TrackTag).where(TrackTag.item_id == 3019)).all() == []
    assert runs()[-1].removed == 3
    # The light list was asked for only where the totals differed.
    light = [params for _method, path, params in synced.library_calls() if "excludeElements" in params
             and "mood" not in params]
    assert {params["type"] for params in light} >= {"9", "10"}


def test_a_partial_run_with_nothing_changed_takes_nothing(client: TestClient, synced: FakePlex) -> None:
    before = tracks()
    synced.calls.clear()
    partial()
    assert tracks().keys() == before.keys()
    # Nothing in the minute before the full run began, nothing since.
    assert runs()[-1].changed == 0 and runs()[-1].removed == 0
    asked = synced.library_calls()
    pages = [params for _method, path, params in asked if path.endswith("/all")
             and params.get("X-Plex-Container-Size") != "0"]
    assert all(any(name.endswith(">>") for name in params) for params in pages)


def test_a_partial_run_failing_leaves_the_catalogue_as_it_was(client: TestClient, synced: FakePlex) -> None:
    synced.touch(3017, title="Fever Glass (Changed)")
    before = tracks()
    synced.fail_after, synced.fail_mode = synced.library_requests + 7, "503"
    partial()
    assert tracks()[3017].title == before[3017].title
    assert runs()[-1].state == "failed" and runs()[-1].error == "plex_busy"


def test_after_a_burst_of_changes_one_full_run_then_quiet_partial_runs(client: TestClient, synced: FakePlex,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Plex touches more than the limit at once: one full run takes it in, and the partial runs after it neither ask
    for another nor fetch the burst again (it lies in the minute looked back)."""
    monkeypatch.setattr(plex_source, "PARTIAL_LIMIT", 3)
    for key in (3001, 3002, 3003, 3004):
        synced.touch(key, rating=1)
    for _ in range(4):
        with SessionLocal() as db:
            catalog.request_partial(db)
        run()
    found = runs()
    assert [one.kind for one in found].count("full") == 2  # the first one, and one for the burst
    assert [(one.kind, one.step) for one in found[1:3]] == [("partial", "many"), ("full", "switch")]
    quiet = found[3:]
    assert len(quiet) == 3 and all(one.kind == "partial" and one.state == "done" for one in quiet)
    assert all(one.step != "many" and one.changed == 0 for one in quiet)
    assert tracks()[3001].rating_count == 1


def test_new_entries_count_once_and_the_minute_looked_back_not_at_all(client: TestClient, synced: FakePlex,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    # Four new tracks carry a new ``addedAt`` and a new ``updatedAt``: four changes, not eight; the newest entries of
    # before, in the minute looked back, are no change either.
    monkeypatch.setattr(plex_source, "PARTIAL_LIMIT", 4)
    for number in range(4):
        synced.add(Item(3100 + number, TRACK, f"Fresh Tune {number}", 2005))
    partial()
    last = runs()[-1]
    assert (last.kind, last.state, last.step) == ("partial", "done", "write")
    assert last.changed == 4
    assert {3100, 3101, 3102, 3103} <= tracks().keys()


def test_an_entry_from_the_future_does_not_blind_the_partial_run(client: TestClient, operator: object,
                                                                 fake_plex: FakePlex,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    # One entry carries a time ten years ahead of Plex's clock; and our own clock is far off, Plex's is the one that
    # counts.
    fake_plex.items[3018].updated = fake_plex.now + 10 * 365 * 86400
    connect(client, fake_plex)
    monkeypatch.setattr(clock, "now", lambda: datetime(2031, 1, 1, tzinfo=UTC))
    run()
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None
        assert all(int(value) <= fake_plex.now for value in row.marks.values())
    fake_plex.touch(3017, title="Fever Glass (Changed Later)")
    partial()
    assert tracks()[3017].title == "Fever Glass (Changed Later)"
    fake_plex.tick(900)
    fake_plex.touch(3003, title="Glasshouse (Changed Later)")
    partial()
    assert tracks()[3003].title == "Glasshouse (Changed Later)"


def test_marks_beyond_plexs_clock_are_capped_and_the_full_run_sets_them_anew(client: TestClient,
                                                                             synced: FakePlex) -> None:
    # Plex's clock went back (or a mark came from an older nexmix): the marks lie far ahead of it.
    ahead = synced.now + 5 * 365 * 86400
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None
        row.marks = {key: ahead for key in row.marks}
        db.commit()
    synced.touch(3017, title="Fever Glass (Changed)")
    partial()
    assert tracks()[3017].title == "Fever Glass (Changed)"
    client.post("/api/v1/catalog/sync")
    run()
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None
        assert row.marks and all(int(value) <= synced.now for value in row.marks.values())


@pytest.mark.parametrize("kind", ["partial", "full"])
def test_a_library_gone_from_plex_is_said_so(client: TestClient, synced: FakePlex, kind: str) -> None:
    synced.servers[0].music = False
    if kind == "partial":
        partial()
    else:
        client.post("/api/v1/catalog/sync")
        run()
    status = client.get("/api/v1/catalog/status").json()
    assert (status["last"]["state"], status["last"]["error"]) == ("failed", "library_gone")
    # The catalogue there was stays.
    assert status["counts"]["tracks"] == 32


def test_something_else_plex_does_not_know_stays_plex_not_found(client: TestClient, synced: FakePlex) -> None:
    # The library is still there; a request of the run is not found for another reason.
    def lost(count: int, path: str) -> None:
        if path.startswith("/library/metadata/"):
            synced.items.clear()

    synced.touch(3017, title="Fever Glass (Changed)")
    synced.hook = lost
    partial()
    assert (runs()[-1].state, runs()[-1].error) == ("failed", "plex_not_found")


def song_fields() -> dict[int, tuple[str, bool, bool, int | None, int | None]]:
    return {one.id: (one.song_key, bool(one.variant), bool(one.shown), one.original_year, one.year)
            for one in tracks().values()}


def every_song_afresh() -> dict[int, tuple[str, bool, bool, int | None, int | None]]:
    """What a computation over the whole catalogue gives, without writing it."""
    with SessionLocal() as db:
        catalog.compute_songs(db, live())
        found = {one.id: (one.song_key, bool(one.variant), bool(one.shown), one.original_year, one.year)
                 for one in db.scalars(select(CatalogTrack).where(CatalogTrack.generation == live()))}
        db.rollback()
    return found


def test_a_partial_run_works_out_only_the_songs_it_touched(client: TestClient, synced: FakePlex,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import songs

    sizes: list[int] = []
    real = songs.songs

    def counted(versions: Iterable[songs.Version]) -> dict[int, songs.Song]:
        chosen = list(versions)
        sizes.append(len(chosen))
        return real(chosen)

    monkeypatch.setattr(songs, "songs", counted)
    partial()
    # Nothing changed: no song is worked out at all.
    assert sizes == []
    # A version of Northern Lights becomes another song; Paper Planes loses its demo.
    synced.touch(3009, title="Southern Lights")
    synced.remove(3027)
    partial()
    # The track, the four others of the song it left and the five of the song that lost one: ten of 31.
    assert sizes == [10]
    assert song_fields() == every_song_afresh()
    found = tracks()
    assert found[3009].shown and found[3009].song_key != found[3001].song_key
    assert 3027 not in found
    # The version shown leaves its song: the song shows another one now, from a later year.
    synced.touch(3001, title="Polar Lights")
    partial()
    assert song_fields() == every_song_afresh()
    found = tracks()
    assert found[3007].shown and found[3007].original_year == 2005


def test_a_pinned_reader_sees_one_state_while_a_partial_run_writes(client: TestClient, synced: FakePlex) -> None:
    synced.remove(3018)
    reader = SessionLocal()
    try:
        generation = catalog.pin(reader)
        assert catalog.counts(reader, generation)["tracks"] == 32
        partial()
        assert tracks().keys() == {key for key in range(3001, 3033)} - {3018}
        # Taken out by the partial run meanwhile, still there for the reader who began before.
        assert catalog.counts(reader, generation)["tracks"] == 32
        assert reader.get(CatalogTrack, (1, generation, 3018)) is not None
    finally:
        reader.close()
    with SessionLocal() as db:
        assert catalog.counts(db, catalog.pin(db))["tracks"] == 31


def test_a_changed_album_year_reaches_its_tracks_and_their_songs(client: TestClient, synced: FakePlex) -> None:
    # "Paper Lanterns" turns out older than Plex said: its tracks and the original year of their songs follow at once.
    synced.touch(2001, year=1997)
    partial()
    found = tracks()
    assert found[3001].year == 1997 and found[3003].year == 1997
    assert found[3009].original_year == 1997 and found[3001].original_year == 1997
    assert song_fields() == every_song_afresh()
    # And the other way: the remaster comes before the album now; the song's year follows, the album still shows.
    synced.touch(2002, year=1990)
    partial()
    found = tracks()
    assert found[3007].year == 1990 and found[3001].original_year == 1990
    assert found[3007].shown and not found[3001].shown
    assert song_fields() == every_song_afresh()


def test_an_artist_renamed_takes_its_songs_along(client: TestClient, synced: FakePlex) -> None:
    synced.touch(1003, title="The Velvet Harbour")
    partial()
    found = tracks()
    # Dax Orlo's "Northern Lights" now belongs to the album artist of the same name as the other one.
    assert found[3020].song_key == found[3001].song_key
    assert song_fields() == every_song_afresh()


def test_disconnecting_stops_a_run_under_way(client: TestClient, synced: FakePlex) -> None:
    asked: list[int] = []

    def drop(count: int, path: str) -> None:
        if path.startswith("/library/metadata/") and not asked:
            asked.append(count)
            assert client.delete("/api/v1/plex").status_code == 204

    synced.hook = drop
    client.post("/api/v1/catalog/sync")
    run()
    # The request under way when the operator let go was the last one.
    assert synced.library_requests == asked[0]
    with SessionLocal() as db:
        assert db.query(CatalogTrack).count() == 0
        assert [(row.active, row.token_enc) for row in db.query(MediaServer)] == [(False, "")]
        assert db.scalars(select(Job.state).where(Job.kind == "catalog_full")).all()[-1] == "done"
        assert db.query(Job).filter(Job.state.in_(("queued", "running"))).count() == 0
    assert [(one.kind, one.state, one.error) for one in runs()] == [("full", "stopped", "disconnected")]


def test_another_library_chosen_stops_a_partial_run_under_way(client: TestClient, synced: FakePlex) -> None:
    asked: list[int] = []

    def switch(count: int, path: str) -> None:
        if path.endswith("/all") and not asked:
            asked.append(count)
            # As ``PUT /api/v1/plex/library`` does with another library of the server.
            with SessionLocal() as db:
                row = plex_link.current(db)
                assert row is not None
                plex_link.choose_library(db, row, plex_link.Section("3", "Music"))
                db.commit()

    synced.hook = switch
    partial()
    # The library request under way finishes; nothing more is asked by the run that was stopped.
    assert synced.library_requests <= asked[0] + 1
    assert (runs()[-1].kind, runs()[-1].state, runs()[-1].error) == ("partial", "stopped", "replaced")


def test_old_runs_and_finished_jobs_are_cleared_away(client: TestClient, synced: FakePlex) -> None:
    now = clock.now()
    with SessionLocal() as db:
        old = now - timedelta(days=31)
        recent = now - timedelta(days=2)
        db.add_all([SyncRun(kind="partial", state="done", serial=1, generation=1, server_id=1, started_at=old, finished_at=old)
                    for _ in range(5)])
        db.add_all([SyncRun(kind="partial", state="done", serial=1, generation=1, server_id=1, started_at=recent,
                            finished_at=recent) for _ in range(1100)])
        db.add_all([Job(kind="catalog_part", ref=0, state="done", created_at=old, finished_at=old) for _ in range(5)])
        db.add_all([Job(kind="catalog_part", ref=0, state=state, created_at=recent, finished_at=recent)
                    for state in ("done", "failed") for _ in range(600)])
        db.add(Job(kind="catalog_part", ref=0, state="queued", created_at=old))
        db.commit()
        catalog.tick(db, now)
        # At most a thousand of each, none older than thirty days.
        assert db.query(SyncRun).count() == 1000
        assert db.query(SyncRun).filter(SyncRun.started_at < now - timedelta(days=30)).count() == 0
        finished = db.query(Job).filter(Job.state.in_(("done", "failed")))
        assert finished.count() == 1000
        assert finished.filter(Job.finished_at < now - timedelta(days=30)).count() == 0
        # What still waits is never cleared away.
        assert db.query(Job).filter(Job.state == "queued").count() >= 1
        # The newest run, the one the card shows, stays.
        assert db.scalars(select(SyncRun.id).order_by(SyncRun.id.desc()).limit(1)).one() is not None
    assert client.get("/api/v1/catalog/status").json()["last"]["kind"] == "partial"


def test_a_partial_run_before_any_full_one_asks_for_the_full_one(client: TestClient, operator: object,
                                                                 fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    with SessionLocal() as db:
        db.execute(Job.__table__.delete())
        db.commit()
        jobs.enqueue(db, catalog.PART, payload={"serial": 1})
        db.commit()
    run()
    assert runs()[-1].kind == "full" and runs()[-1].state == "done"


# --- The clock -----------------------------------------------------------------------------------------------------


@pytest.fixture
def utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "local_zone", lambda: UTC)


def test_the_clock_asks_for_the_first_run_then_every_fifteen_minutes(client: TestClient, operator: object,
                                                                     fake_plex: FakePlex, utc: None,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "now", lambda: start)
    connect(client, fake_plex)
    with SessionLocal() as db:
        assert catalog.tick(db, start) is None  # the first run waits in the queue already
    run()
    with SessionLocal() as db:
        assert catalog.tick(db, start + timedelta(minutes=14)) is None
        assert catalog.tick(db, start + timedelta(minutes=15)) == "partial"
        assert catalog.tick(db, start + timedelta(minutes=16)) is None  # one waits already
    run()


def test_the_clock_runs_the_whole_catalogue_at_night_once(client: TestClient, synced: FakePlex, utc: None) -> None:
    with SessionLocal() as db:
        first = runs()[-1]
        evening = datetime(2026, 11, 13, 22, 0, tzinfo=UTC)
        db.execute(SyncRun.__table__.update().values(started_at=evening))
        db.commit()
        assert catalog.tick(db, datetime(2026, 11, 14, 3, 29, tzinfo=UTC)) == "partial"
    run()
    with SessionLocal() as db:
        db.execute(SyncRun.__table__.update().values(started_at=datetime(2026, 11, 14, 3, 20, tzinfo=UTC)))
        db.commit()
        assert catalog.tick(db, datetime(2026, 11, 14, 3, 31, tzinfo=UTC)) == "full"
    assert first.kind == "full"
    run()
    with SessionLocal() as db:
        db.execute(SyncRun.__table__.update().where(SyncRun.state == "done")
                   .values(started_at=datetime(2026, 11, 14, 3, 31, tzinfo=UTC)))
        db.commit()
        # The night's run is done: the next look an hour later asks for a partial one, not another full one.
        assert catalog.tick(db, datetime(2026, 11, 14, 4, 31, tzinfo=UTC)) == "partial"


def test_the_night_time_is_the_operators_choice(client: TestClient, synced: FakePlex, utc: None) -> None:
    assert client.put("/api/v1/settings", json={"catalog_full_at": "25:00"}).status_code == 422
    assert client.put("/api/v1/settings", json={"catalog_full_at": "05:15"}).json()["catalog_full_at"] == "05:15"
    with SessionLocal() as db:
        db.execute(SyncRun.__table__.update().values(started_at=datetime(2026, 11, 14, 4, 0, tzinfo=UTC)))
        db.commit()
        assert catalog.tick(db, datetime(2026, 11, 14, 4, 30, tzinfo=UTC)) == "partial"
    run()
    with SessionLocal() as db:
        db.execute(SyncRun.__table__.update().values(started_at=datetime(2026, 11, 14, 5, 10, tzinfo=UTC)))
        db.commit()
        assert catalog.tick(db, datetime(2026, 11, 14, 5, 16, tzinfo=UTC)) == "full"
    run()
    assert client.put("/api/v1/settings", json={"catalog_full_at": ""}).json()["catalog_full_at"] == ""
    with SessionLocal() as db:
        db.execute(SyncRun.__table__.update().values(started_at=datetime(2026, 11, 15, 2, 0, tzinfo=UTC)))
        db.commit()
        assert catalog.tick(db, datetime(2026, 11, 15, 5, 30, tzinfo=UTC)) == "partial"


def test_without_plex_the_clock_does_nothing(client: TestClient, operator: object) -> None:
    with SessionLocal() as db:
        assert catalog.tick(db, datetime(2026, 11, 14, 3, 31, tzinfo=UTC)) is None
        assert db.query(Job).count() == 0


# --- Sonic analysis ------------------------------------------------------------------------------------------------


def analysis(client: TestClient) -> dict[str, object]:
    return dict(client.get("/api/v1/catalog/status").json()["analysis"])


def test_the_sonic_analysis_off_running_and_on(client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    run()
    assert analysis(client) == {"state": "off", "percent": None}
    fake_plex.analysis = True
    for key, item in fake_plex.items.items():
        if item.kind == TRACK and key % 4 == 0:
            item.analysed = True
    client.post("/api/v1/catalog/sync")
    run()
    state = analysis(client)
    assert state["state"] == "running" and 15 <= int(state["percent"]) <= 35  # type: ignore[call-overload]
    for item in fake_plex.items.values():
        item.analysed = True
    client.post("/api/v1/catalog/sync")
    run()
    assert analysis(client) == {"state": "on", "percent": 100}


def test_the_analysis_is_measured_again_by_a_partial_run_after_an_hour(client: TestClient, synced: FakePlex,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    synced.analysis = True
    for item in synced.items.values():
        item.analysed = True
    partial()
    assert analysis(client)["state"] == "off"
    later = clock.now() + timedelta(hours=2)
    monkeypatch.setattr(clock, "now", lambda: later)
    partial()
    assert analysis(client) == {"state": "on", "percent": 100}


# --- Who sees what -------------------------------------------------------------------------------------------------


def test_a_member_sees_the_state_of_the_catalogue(synced: FakePlex) -> None:
    with new_client(make_account("mila")) as browser:
        status = browser.get("/api/v1/catalog/status")
        assert status.status_code == 200
        assert status.json()["counts"]["tracks"] == 32
        assert "server_name" not in status.text and synced.account_token not in status.text


def test_the_settings_say_when_the_night_run_is(client: TestClient, operator: object) -> None:
    assert client.get("/api/v1/settings").json()["catalog_full_at"] == "03:30"
    with SessionLocal() as db:
        assert settings_service.get(db, "catalog_full_at") == "03:30"


# --- Schema 1 to 2 -------------------------------------------------------------------------------------------------


def test_a_database_of_schema_one_gets_the_catalogue_and_keeps_its_accounts(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "nexmix.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript((RECORDS / "v1.sql").read_text(encoding="utf-8"))
        connection.execute("INSERT INTO settings (key, value) VALUES ('kept', '\"yes\"')")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine)
    made: list[str] = []
    monkeypatch.setattr(backups, "create", lambda **kwargs: made.append(kwargs.get("note", "")) or path)
    database.init_db()
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"media_servers", "sync_runs", "artists", "albums", "tracks", "tags", "track_tags", "album_tags",
                "artist_tags"} <= names
        assert connection.execute("SELECT value FROM settings WHERE key = 'kept'").fetchone() == ('"yes"',)
    assert made == [f"before schema {database.SCHEMA_VERSION}"]


def test_only_one_connection_row_ever(client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    connect(client, fake_plex)
    with SessionLocal() as db:
        rows = db.query(MediaServer).all()
        assert len(rows) == 1 and rows[0].serial == 2
