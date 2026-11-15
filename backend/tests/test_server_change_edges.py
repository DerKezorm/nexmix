"""The edges of a change of server and of the step from 0.1.0: an entry found only by its title stays where it was, a
smart playlist is never made as an ordinary one on another server, a connection of 0.1.0 without a machine id keeps its
playlists, a key that is a large number gets a number below the limit, and a step of the schema that leaves rows
without their parent breaks off whole."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db as database
from app import migrate_9
from app.config import get_settings
from app.db import SessionLocal
from app.models import Account, MediaServer, ServerPlaylist
from app.services import backups, media
from app.services import sync as server_sync

from .conftest import new_client
from .fakeplex import FakePlex
from .fakeplex.library import TRACK, Item, made_up_library
from .fakeplex.lists import FakeList
from .plex_world import connect, make, mirror, sync
from .test_migration_0_1 import ATTIC, STUDIO, _lists, _now, _old_database, _operator, _place, _rows, _stand_in

SMART_KEY_ON_STUDIO = 90_031


def _quiet(fake: FakePlex, rounds: int = 3) -> None:
    for _round in range(rounds):
        fake.tick(30)
        with SessionLocal() as db:
            server_sync.request_all(db)
        sync(fake, look_at_plex=True)


def _made_in_plex(fake: FakePlex) -> list[str]:
    """The titles of the playlists nexmix asked Plex to make."""
    return [params.get("title", "") for method, path, params in fake.lists.writes
            if method == "POST" and path == "/playlists"]


# --- Only sure findings after a change of server ---------------------------------------------------------------------


def test_an_entry_found_by_its_title_alone_stays_in_nexmix_with_its_hint(client: TestClient, operator: Account,
                                                                          fake_plex: FakePlex) -> None:
    """The song of the entry is not on the new server; a track of the same (generic) title by somebody else is. 0.1.0
    left the entry in nexmix with the hint ``elsewhere``; it never sent the other song to Plex in its place."""
    connect(client, fake_plex)
    playlist = make(client, "Intro Test", [3006, 3001])["id"]
    sync(fake_plex)
    library = made_up_library()
    del library[3006]
    # Another song called "Intro" on the new server, by another artist, a second longer.
    library[3040] = Item(3040, TRACK, "Intro", 2005, duration=41_000, rating=2_000)
    fake_plex.items.clear()
    fake_plex.items.update(library)
    fake_plex.lists.lists.clear()
    fake_plex.servers[0].machine = ATTIC
    connect(client, fake_plex)
    for _round in range(3):
        fake_plex.tick(30)
        sync(fake_plex, look_at_plex=True)
    items = client.get(f"/api/v1/playlists/{playlist}").json()["items"]
    shown = [(entry["title"], entry["plex_hint"]) for entry in items]
    assert shown == [("Intro", "elsewhere"), ("Northern Lights", None)], shown
    there = [[fake_plex.items[track].title for track in one.tracks()] for one in fake_plex.lists.lists.values()]
    assert there == [["Northern Lights"]], there


# --- Smart playlists stay smart ----------------------------------------------------------------------------------------


def test_a_smart_playlist_is_not_made_on_another_server_and_is_taken_up_back_on_its_own(
        client: TestClient, operator: Account, fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    smart = fake_plex.lists.make("Smart Mix", [3001, 3003, 3013], smart=True)
    fake_plex.tick()
    sync(fake_plex, look_at_plex=True)
    card = next(one for one in client.get("/api/v1/playlists").json()["playlists"] if one["name"] == "Smart Mix")
    assert mirror(card["id"]).smart
    home, origin = dict(fake_plex.lists.lists), fake_plex.servers[0].machine
    # Another server in its place: nothing of it there.
    fake_plex.lists.lists.clear()
    fake_plex.servers[0].machine = ATTIC
    connect(client, fake_plex)
    _quiet(fake_plex)
    assert fake_plex.lists.lists == {}
    assert _made_in_plex(fake_plex) == []
    detail = client.get(f"/api/v1/playlists/{card['id']}").json()
    assert (detail["plex"]["state"], detail["plex"]["error"], detail["plex"]["smart"]) == (
        "error", server_sync.SMART_NOT_HERE, True)
    assert len(detail["items"]) == 3
    # Back on its own server: the same smart playlist, read, nothing made.
    fake_plex.lists.lists.update(home)
    fake_plex.servers[0].machine = origin
    connect(client, fake_plex)
    _quiet(fake_plex)
    assert _made_in_plex(fake_plex) == []
    assert [one.title for one in fake_plex.lists.lists.values()].count("Smart Mix") == 1
    back = mirror(card["id"])
    assert (back.ext_key, back.smart, back.state) == (str(smart.key), True, "synced")


def test_a_smart_playlist_gone_from_its_own_server_meanwhile_is_not_made_there_as_an_ordinary_one(
        client: TestClient, operator: Account, fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    smart = fake_plex.lists.make("Smart Mix", [3001, 3003], smart=True)
    fake_plex.tick()
    sync(fake_plex, look_at_plex=True)
    card = next(one for one in client.get("/api/v1/playlists").json()["playlists"] if one["name"] == "Smart Mix")
    home, origin = dict(fake_plex.lists.lists), fake_plex.servers[0].machine
    fake_plex.lists.lists.clear()
    fake_plex.servers[0].machine = ATTIC
    connect(client, fake_plex)
    _quiet(fake_plex)
    # Deleted in Plex on its own server while nexmix was on the other one.
    del home[smart.key]
    fake_plex.lists.lists.update(home)
    fake_plex.servers[0].machine = origin
    connect(client, fake_plex)
    _quiet(fake_plex)
    assert _made_in_plex(fake_plex) == []
    assert fake_plex.lists.lists == {}
    detail = client.get(f"/api/v1/playlists/{card['id']}").json()
    assert (detail["plex"]["state"], detail["plex"]["error"], detail["plex"]["smart"]) == (
        "error", server_sync.SMART_NOT_HERE, True)


def _came_from_studio(connection: sqlite3.Connection) -> None:
    """0.1.0 had moved the smart playlist "Alles Gute" from the studio to the attic: its copy on the studio waits in
    ``elsewhere``, the row itself has no key and stood at an error (0.1.0 could not read it on the attic)."""
    copy = {STUDIO: {"key": SMART_KEY_ON_STUDIO, "guid": "com.plexapp.agents.none://smart-on-the-studio",
                     "base": None}}
    connection.execute("UPDATE plex_playlists SET rating_key = NULL, guid = '', seen = NULL, state = 'error', "
                       "error = 'internal_error', elsewhere = ? WHERE playlist_id = 4", (json.dumps(copy),))


@pytest.fixture
def smart_from_studio(fake_plex: FakePlex, tmp_path: Path) -> dict[str, Any]:
    state = _stand_in(fake_plex)
    # The attic does not have it (it was the studio's).
    fake_plex.lists.lists = {key: one for key, one in fake_plex.lists.lists.items() if one.title != "Alles Gute"}
    old = tmp_path / "nexmix-0.1.0.db"
    _old_database(old, fake_plex.account_token)
    with closing(sqlite3.connect(old)) as connection:
        _came_from_studio(connection)
        connection.commit()
    _place(old)
    database.init_db()
    server_sync.forget()
    return state


def test_the_step_from_0_1_0_keeps_a_smart_playlist_smart_on_every_server(smart_from_studio: dict[str, Any],
                                                                          fake_plex: FakePlex) -> None:
    with SessionLocal() as db:
        copies = {(row.server_id, row.ext_key): row for row in db.scalars(select(ServerPlaylist).where(
            ServerPlaylist.playlist_id == 4))}
        studio = db.scalars(select(MediaServer.id).where(MediaServer.machine_id == STUDIO)).one()
    assert set(copies) == {(1, None), (studio, str(SMART_KEY_ON_STUDIO))}
    here, left = copies[(1, None)], copies[(studio, str(SMART_KEY_ON_STUDIO))]
    assert (here.smart, here.smart_total) == (True, 3)
    assert (left.smart, left.smart_total, left.resume) == (True, 3, True)
    # On the attic it rests: nothing is made or written there.
    _quiet(fake_plex)
    assert fake_plex.lists.writes == []
    with new_client(_operator()) as client:
        detail = client.get("/api/v1/playlists/4").json()
    assert (detail["plex"]["state"], detail["plex"]["error"]) == ("error", server_sync.SMART_NOT_HERE)
    # Back on the studio: its smart playlist is taken up, not made a second time as an ordinary one.
    studio_lists = _lists(smart_from_studio["studio"])
    studio_lists[SMART_KEY_ON_STUDIO] = FakeList(key=SMART_KEY_ON_STUDIO, title="Alles Gute", smart=True,
                                                 rule=[3001, 3009, 3026], updated_at=fake_plex.now,
                                                 added_at=fake_plex.now,
                                                 guid="com.plexapp.agents.none://smart-on-the-studio")
    with new_client(_operator()) as client:
        fake_plex.lists.lists.clear()
        fake_plex.lists.lists.update(studio_lists)
        fake_plex.items.clear()
        fake_plex.items.update(made_up_library())
        fake_plex.servers[0].machine = STUDIO
        connect(client, fake_plex)
        _quiet(fake_plex)
        detail = client.get("/api/v1/playlists/4").json()
    assert "Alles Gute" not in _made_in_plex(fake_plex)
    assert [one.title for one in fake_plex.lists.lists.values()].count("Alles Gute") == 1
    assert (detail["plex"]["state"], detail["plex"]["smart"]) == ("synced", True)
    assert [entry["title"] for entry in detail["items"]] == ["Northern Lights", "Northern Lights", "Paper Planes"]


# --- A connection of 0.1.0 without a machine id ----------------------------------------------------------------------


def test_a_connection_without_a_machine_id_keeps_its_playlists(fake_plex: FakePlex, tmp_path: Path) -> None:
    """0.1.0 compared the rows' machine with the connection's, also when both were empty: those rows were its own. The
    step keeps them on server 1 with their keys, and the sync after it takes in nothing anew and writes nothing."""
    _stand_in(fake_plex)
    old = tmp_path / "nexmix-0.1.0.db"
    _old_database(old, fake_plex.account_token)
    with closing(sqlite3.connect(old)) as connection:
        connection.execute("UPDATE plex_connection SET machine_id = ''")
        connection.execute("UPDATE plex_playlists SET machine = '' WHERE machine = ?", (ATTIC,))
        # A track Plex refused, gone from the library since: only the copy's state names it.
        connection.execute("UPDATE plex_playlists SET refused = ? WHERE playlist_id = 1",
                           (json.dumps({"8999": {"tries": 5, "next": "2099-01-01T00:00:00+00:00"}}),))
        connection.commit()
        keys = {row[0]: str(row[1]) if row[1] is not None else None
                for row in connection.execute("SELECT playlist_id, rating_key FROM plex_playlists")}
    playlists_before = _rows(old, "playlists")
    _place(old)
    database.init_db()
    with SessionLocal() as db:
        first = {row.playlist_id: row for row in db.scalars(select(ServerPlaylist).where(ServerPlaylist.server_id == 1))}
    assert {playlist: row.ext_key for playlist, row in first.items()} == keys
    assert {(row.resume, row.settle) for row in first.values()} == {(False, "")}
    with SessionLocal() as db:
        # The states of these copies are server 1's: their tracks keep their numbers there.
        assert media.keys(db, 1, media.TRACK, [8999]) == {8999: "8999"}
    server_sync.forget()
    _quiet(fake_plex)
    assert fake_plex.lists.writes == []
    assert [row[:-1] for row in _now("playlists")] == playlists_before, "no playlist taken in twice"
    with SessionLocal() as db:
        states = {row.state for row in db.scalars(select(ServerPlaylist).where(ServerPlaylist.server_id == 1))}
    assert states == {"synced"}


# --- Numbers below the limit -------------------------------------------------------------------------------------------


def test_a_key_that_is_a_large_number_gets_a_number_below_the_limit(client: TestClient, operator: Account) -> None:
    limit = media.NUMBER_LIMIT
    with SessionLocal() as db:
        db.add(MediaServer(id=1, kind="plex", active=True, machine_id="m1", marks={}))
        db.add(MediaServer(id=2, kind="plex", active=True, machine_id="m2", marks={}))
        db.commit()
        big = media.numbers(db, 2, media.TRACK, [str(limit), "5000000000000"])
        later = media.numbers(db, 1, media.TRACK, ["abc-guid"])
        # One just below the limit keeps itself; the next new one may not go beyond.
        edge = media.numbers(db, 2, media.TRACK, [str(limit - 1)])
        after = media.numbers(db, 1, media.TRACK, ["after-the-edge", "and-one-more"])
        db.commit()
    given = [*big.values(), *later.values(), *after.values()]
    assert edge == {str(limit - 1): limit - 1}
    assert all(0 < number < limit for number in given), given
    assert len(set(given)) == len(given) and limit - 1 not in given
    with SessionLocal() as db:
        assert media.known_numbers(db, 1, media.TRACK, ["after-the-edge"]) == {"after-the-edge": after["after-the-edge"]}
    # The API takes every number nexmix gives, and none beyond.
    made = client.post("/api/v1/playlists", json={"name": "Grenze"}).json()["id"]
    assert client.post(f"/api/v1/playlists/{made}/items", json={"tracks": given}).status_code != 422
    assert client.post(f"/api/v1/playlists/{made}/items", json={"tracks": [limit + 1]}).status_code == 422


# --- A step of the schema that leaves broken rows ----------------------------------------------------------------------


def _schema(path: Path) -> tuple[int, set[str]]:
    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return version, names


def test_a_step_that_leaves_rows_without_their_parent_breaks_off_whole(fake_plex: FakePlex, tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch,
                                                                       caplog: pytest.LogCaptureFixture) -> None:
    _stand_in(fake_plex)
    old = tmp_path / "nexmix-0.1.0.db"
    _old_database(old, fake_plex.account_token)
    step = migrate_9.run

    def broken(connection: Any) -> None:
        step(connection)
        # A copy on a server there is not.
        connection.exec_driver_sql("INSERT INTO server_playlists (playlist_id, server_id, guid, smart, state, error, "
                                   "tries, recheck, gone_there, resume) VALUES (1, 99, '', 0, 'synced', '', 0, 0, "
                                   "0, 0)")

    monkeypatch.setattr(migrate_9, "run", broken)
    today = tmp_path / "today.db"
    shutil.copyfile(get_settings().database_path, today)
    _place(old)
    try:
        with caplog.at_level(logging.ERROR, logger="nexmix.db"), pytest.raises(database.MigrationBroken):
            database.init_db()
        version, names = _schema(get_settings().database_path)
    finally:
        # The tests after this one start from a database of today.
        _place(today)
    assert version == 8
    assert {"plex_connection", "plex_playlists"} <= names and "media_servers" not in names
    assert any("migration broke off" in record.getMessage() and "server_playlists" in record.getMessage()
               for record in caplog.records)


def test_a_row_without_its_parent_from_before_does_not_stop_the_step(fake_plex: FakePlex, tmp_path: Path) -> None:
    """What 0.1.0 left broken is no fault of the step: it stays as it was, and the step goes through."""
    _stand_in(fake_plex)
    old = tmp_path / "nexmix-0.1.0.db"
    _old_database(old, fake_plex.account_token)
    with closing(sqlite3.connect(old)) as connection:
        connection.execute("INSERT INTO hidden_playlists (account_id, playlist_id, created_at) VALUES "
                           "(1, 9999, '2026-11-15 10:00:00.000000')")
        connection.commit()
        before = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert before
    _place(old)
    database.init_db()
    version, _names = _schema(get_settings().database_path)
    assert version == database.SCHEMA_VERSION
    with closing(sqlite3.connect(get_settings().database_path)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == before


# --- The manifest of a backup ----------------------------------------------------------------------------------------


def test_a_backup_names_the_running_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backups, "__version__", "9.8.7")
    made = backups.create(kind=backups.MANUAL)
    with zipfile.ZipFile(made) as archive:
        manifest = json.loads(archive.read(backups.MANIFEST))
    assert manifest["version"] == "9.8.7"
