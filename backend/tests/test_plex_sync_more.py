"""Around the sync of the playlists: what the browsers hear of it, Plex's stream of events, the step of the schema
that brings older playlists along, and the clocks of the background stopping cleanly."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import sessionmaker

from app import db as database
from app.models import Account, ServerPlaylist
from app.routers import playlists as router
from app.services import catalog, plex, plex_events, plex_link
from app.services import sync as server_sync

from .fakeplex import FakePlex
from .plex_world import connect, make, plex_key, sync, tick
from .test_playlist_events import Listener
from .test_schema import RECORDS, scratch  # noqa: F401 - the fixture of a database of its own


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex,
          monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePlex]:
    monkeypatch.setattr(router, "STREAM_SECONDS", 4.0)
    monkeypatch.setattr(router, "PING_SECONDS", 0.3)
    connect(client, fake_plex)
    server_sync.forget()
    yield fake_plex


# --- What the browsers hear --------------------------------------------------------------------------------------------


def test_an_open_playlist_hears_what_came_from_plex_and_how_the_sync_stands(client: TestClient,
                                                                            world: FakePlex) -> None:
    playlist = make(client, "Live", [3001])
    sync(world)
    listener = Listener(client, playlist["id"])
    hello = listener.wait_for("hello")
    world.lists.add(plex_key(playlist["id"]), [3013])
    world.tick()
    sync(world, look_at_plex=True)
    change = listener.wait_for("items")
    assert change["op"] == "sync" and change["version"] == hello["version"] + 1
    assert [entry["track"] for entry in change["entries"]] == [3013]
    assert change["entries"][0]["from_plex"] is True and change["by"] is None
    assert len(change["order"]) == 2 and change["name"] == "Live"
    state = listener.wait_for("plex")
    assert state["state"] == "synced"


def test_a_playlist_deleted_in_plex_tells_its_browsers(client: TestClient, world: FakePlex) -> None:
    playlist = make(client, "Bald weg", [3001])
    sync(world)
    listener = Listener(client, playlist["id"])
    listener.wait_for("hello")
    world.lists.delete(plex_key(playlist["id"]))
    sync(world, look_at_plex=True)
    gone = listener.wait_for("playlist")
    assert gone["op"] == "deleted" and gone["plex"] is True


# --- Plex's stream of events -------------------------------------------------------------------------------------------


def test_the_stream_says_look_now_for_playlists_made_renamed_or_deleted() -> None:
    def lines(state: int, kind: int = 15) -> list[str]:
        entry = {"TimelineEntry": {"itemID": "7", "state": state, "type": kind, "sectionID": "-1"}}
        return ["event: timeline", f"data: {json.dumps(entry)}", ""]

    for state, kind, wanted in ((0, 15, True), (5, 15, True), (9, 15, True), (3, 15, False), (5, 10, False)):
        heard: list[int] = []
        plex_events.read(lines(state, kind), lambda found=heard: found.append(1), lambda: False)
        assert bool(heard) is wanted, (state, kind)
    # A list of entries, a ping, rubbish: no harm.
    both = {"TimelineEntry": [{"type": 10, "state": 5}, {"type": 15, "state": 9}]}
    heard = []
    plex_events.read(["event: timeline", f"data: {json.dumps(both)}", "", "event: ping", "data: {}", "",
                      "event: timeline", "data: not json", ""], lambda: heard.append(1), lambda: False)
    assert heard == [1]


def test_the_stream_of_the_stand_in_plex_brings_a_look_at_once(client: TestClient, world: FakePlex) -> None:
    world.lists.make("Neu in Plexamp", [3001])
    sync(world, look_at_plex=True)
    world.lists.rename(next(iter(world.lists.lists)), "Umbenannt in Plexamp")
    with database.SessionLocal() as db:
        connection = plex_link.current(db)
        assert connection is not None
        server = plex_link.server_of(db, connection)
    # The look was just made: the next one is 15 seconds away, unless the stream says otherwise.
    assert tick() == 0
    with server:
        count = plex_events.listen(server, lambda: False)
    assert count >= 2
    assert tick() == 1, "a look at once"
    # The token went in a header, never in an address.
    asked = [(path, params) for _method, path, params in world.library_calls() if path == plex_events.PATH]
    assert asked and all("X-Plex-Token" not in params for _path, params in asked)


def test_the_listener_opens_the_stream_again_and_stops(world: FakePlex, monkeypatch: pytest.MonkeyPatch) -> None:
    # After a stream that brought something, at once again; only failures in a row wait longer.
    monkeypatch.setattr(plex_events, "WAITS", (0.05, 30))
    opened = threading.Event()
    seen: list[int] = []
    real = plex_events.listen

    def counting(server: plex.PlexServer, stop: Any, server_id: int | None = None) -> int:
        seen.append(1)
        if len(seen) >= 3:
            opened.set()
        # Each stream brings an event.
        world.lists.make(f"Neu {len(seen)}")
        return real(server, stop, server_id)

    monkeypatch.setattr(plex_events, "listen", counting)
    listener = plex_events.Listener()
    listener.start()
    try:
        assert opened.wait(10), "opened again after each end"
    finally:
        listener.stop()
    assert listener._thread is not None
    listener._thread.join(5)
    assert not listener._thread.is_alive()


def test_a_failing_stream_waits_longer_each_time(world: FakePlex, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    listener = plex_events.Listener()

    def wait(seconds: float | None = None) -> bool:
        waits.append(seconds or 0)
        if len(waits) >= 4:
            listener._stop.set()
        return listener._stop.is_set()

    monkeypatch.setattr(listener._stop, "wait", wait)
    world.fail_after = 0
    world.fail_mode = "drop"
    listener._loop()
    assert waits == [2, 5, 10, 30]


def test_a_stream_that_ends_after_every_ping_waits_longer_each_time(world: FakePlex,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # The stand-in ends its stream after a ping, as a proxy might: no events, open for a moment only.
    waits: list[float] = []
    listener = plex_events.Listener()

    def wait(seconds: float | None = None) -> bool:
        waits.append(seconds or 0)
        if len(waits) >= 6:
            listener._stop.set()
        return listener._stop.is_set()

    monkeypatch.setattr(listener._stop, "wait", wait)
    listener._loop()
    assert waits == [2, 5, 10, 30, 60, 60]


def test_the_count_of_tries_starts_over_only_after_a_good_stream() -> None:
    assert plex_events.next_tries(3, 1, 0.1) == 0
    assert plex_events.next_tries(3, 0, plex_events.STEADY) == 0
    assert plex_events.next_tries(3, 0, plex_events.STEADY - 1) == 4
    assert plex_events.next_tries(0, 0, 0.0) == 1


# --- Older playlists come along ---------------------------------------------------------------------------------------


def test_playlists_of_an_older_nexmix_go_to_plex_after_the_update(scratch: Path) -> None:  # noqa: F811
    with closing(sqlite3.connect(scratch)) as connection:
        connection.executescript((RECORDS / "v3.sql").read_text(encoding="utf-8"))
        connection.execute("PRAGMA user_version = 3")
        connection.execute("INSERT INTO users (id, name, display_name, whats_new_seen, role, sign_in, password_hash, "
                           "email, language, created_at, failed_logins, totp_secret_enc, totp_recovery, "
                           "totp_last_step) VALUES (1, 'otto', '', '', 'operator', 'password', 'x', '', 'de', "
                           "'2026-11-01 10:00:00.000000', 0, '', '', 0)")
        for number, deleted in ((1, None), (2, "2026-11-02 10:00:00.000000")):
            connection.execute("INSERT INTO playlists (id, owner_id, name, description, version, created_at, "
                               "updated_at, deleted_at) VALUES (?, 1, ?, '', 1, '2026-11-01 10:00:00.000000', "
                               "'2026-11-01 10:00:00.000000', ?)", (number, f"Alt {number}", deleted))
        connection.execute("INSERT INTO playlist_items (playlist_id, track_id, position, added_by_user, added_by_name, "
                           "added_at, title, artist, album, album_id, duration) VALUES (1, 3001, 0, 1, 'otto', "
                           "'2026-11-01 10:00:00.000000', 'T', 'A', 'B', 2001, 1000)")
        connection.commit()
    database.init_db()
    session = sessionmaker(bind=database.engine)
    with session() as db:
        # No Plex was connected: nothing was made anywhere, and the playlists wait for the first server connected,
        # which takes them (``sync.moved``) and makes them there; their entries are kept as they were.
        assert db.scalars(select(ServerPlaylist)).all() == []
        assert db.execute(text("SELECT id, source_server_id FROM playlists ORDER BY id")).all() == [(1, None),
                                                                                                    (2, None)]
        assert db.execute(text("SELECT from_plex, track_id FROM playlist_items")).one() == (0, 3001)


# --- The clocks stop cleanly -------------------------------------------------------------------------------------------


def test_stopping_the_catalogue_clock_in_the_middle_of_a_tick_logs_no_error(caplog: pytest.LogCaptureFixture,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop cancels the wait for the thread of the tick. The session must not be closed from the loop while the
    thread is in the middle of opening its connection: that was an ``IllegalStateChangeError`` in the log."""
    inside = threading.Event()
    release = threading.Event()

    def hold(*_args: Any) -> None:
        if threading.current_thread() is not threading.main_thread() and not release.is_set():
            inside.set()
            release.wait(5)

    def slow_tick(db: Any, now: Any = None) -> None:
        db.execute(text("SELECT 1"))

    monkeypatch.setattr(catalog, "tick", slow_tick)
    event.listen(database.engine, "connect", hold)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(catalog.run_forever(stop))
        assert await asyncio.to_thread(inside.wait, 5)
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await asyncio.sleep(0.3)

    try:
        with caplog.at_level(logging.ERROR, logger="nexmix.catalog"):
            asyncio.run(scenario())
    finally:
        release.set()
        event.remove(database.engine, "connect", hold)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR], caplog.text


def test_stopping_the_sync_clock_in_the_middle_of_a_tick_logs_no_error(caplog: pytest.LogCaptureFixture,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    inside = threading.Event()
    release = threading.Event()

    def hold(*_args: Any) -> None:
        if threading.current_thread() is not threading.main_thread() and not release.is_set():
            inside.set()
            release.wait(5)

    def slow_tick(db: Any, now: Any = None) -> int:
        db.execute(text("SELECT 1"))
        return 0

    monkeypatch.setattr(server_sync, "tick", slow_tick)
    event.listen(database.engine, "connect", hold)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(server_sync.run_forever(stop))
        assert await asyncio.to_thread(inside.wait, 5)
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await asyncio.sleep(0.3)

    try:
        with caplog.at_level(logging.ERROR, logger="nexmix.plexsync"):
            asyncio.run(scenario())
    finally:
        release.set()
        event.remove(database.engine, "connect", hold)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR], caplog.text


def test_httpx_is_the_client_of_the_stream() -> None:
    # The stream goes through the same client as every request to Plex: no redirects, certificates checked.
    client = plex.http_client(5.0)
    try:
        assert client.follow_redirects is False
        assert isinstance(client, httpx.Client)
    finally:
        client.close()
