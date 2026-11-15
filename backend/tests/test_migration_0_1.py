"""A database of nexmix 0.1.0 (schema 8) comes over whole: brought back from a backup of 0.1.0, the start takes it to
media servers (schema 9) and every playlist, entry, common state of the sync, write state, hint and guest's entry means
what it meant. The sync that follows finds both sides equal and writes nothing to Plex.

The rows (``fixtures/nexmix-0.1.0/v8-rows.sql``) were made with the code of 0.1.0 against the stand-in Plex: a server
(``attic``) with three playlists that came from another one (``studio``, remembered in ``elsewhere``), a track that is
not on ``attic`` (its entries say ``elsewhere``), entries added in Plexamp, a smart playlist and one made in Plexamp,
a playlist in the trash, a share, a hidden playlist, hidden suggestions, a guest with an entry of its own. The state of
the stand-in Plex at that moment is ``plex.json``. Every name is made up; the token is sealed at the run."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import db as database
from app.config import get_settings
from app.db import SessionLocal
from app.models import MediaServer, ServerItem, ServerPlaylist
from app.security import encrypt_secret
from app.services import backups, media, plex_link
from app.services import sync as server_sync

from .conftest import PASSWORD, new_client
from .fakeplex import FakePlex
from .fakeplex.library import Item
from .fakeplex.lists import FakeList
from .plex_world import connect, sync

HERE = Path(__file__).parent
ROWS = HERE / "fixtures" / "nexmix-0.1.0" / "v8-rows.sql"
PLEX = HERE / "fixtures" / "nexmix-0.1.0" / "plex.json"
SCHEMA_8 = HERE / "schema" / "v8.sql"
ATTIC, STUDIO = "fake-machine-attic", "fake-machine-studio"
#: The tables whose rows must come over exactly as they were.
SAME_TABLES = ("users", "playlists", "playlist_items", "playlist_shares", "hidden_playlists", "hidden_suggestions",
               "guest_invites", "guests", "guest_entries", "settings")


def _lists(found: list[dict[str, Any]]) -> dict[int, FakeList]:
    return {int(one["key"]): FakeList(key=int(one["key"]), title=one["title"], smart=bool(one["smart"]),
                                      items=[(int(item), int(track)) for item, track in one["items"]],
                                      rule=[int(track) for track in one["rule"]], updated_at=int(one["updated_at"]),
                                      added_at=int(one["added_at"]), guid=one["guid"],
                                      stale_count=int(one["stale_count"]))
            for one in found}


def _stand_in(fake: FakePlex) -> dict[str, Any]:
    """The stand-in Plex as it was when the database was made."""
    state = json.loads(PLEX.read_text(encoding="utf-8"))
    fake.now = int(state["now"])
    fake.items.clear()
    for one in state["items"]:
        item = Item(**one)
        fake.items[item.key] = item
    fake.lists.lists.clear()
    fake.lists.lists.update(_lists(state["lists"]))
    fake.lists.next_key, fake.lists.next_item = int(state["next_key"]), int(state["next_item"])
    fake.servers[0].machine = state["machine"]
    return state


def _old_database(path: Path, token: str) -> None:
    """The database of 0.1.0: its schema, its rows, the token sealed with this server's secret, a password to sign in
    with."""
    from app.security import hash_password

    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(SCHEMA_8.read_text(encoding="utf-8"))
        connection.executescript(ROWS.read_text(encoding="utf-8"))
        connection.execute("UPDATE plex_connection SET token_enc = ?", (encrypt_secret(token, "plex"),))
        connection.execute("UPDATE users SET password_hash = ?", (hash_password(PASSWORD),))
        connection.execute("PRAGMA user_version = 8")
        connection.commit()


def _rows(path: Path, table: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return sorted(connection.execute(f"SELECT * FROM {table}").fetchall(), key=repr)  # noqa: S608


def _place(source: Path) -> None:
    """The database file of the running nexmix replaced by ``source`` (nothing holds it open: no pool)."""
    target = get_settings().database_path
    for leftover in (target, target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
        leftover.unlink(missing_ok=True)
    shutil.copyfile(source, target)


@pytest.fixture
def restored(fake_plex: FakePlex, tmp_path: Path) -> dict[str, Any]:
    """A backup of 0.1.0 brought back and started: the database migrated, the stand-in Plex as it was."""
    state = _stand_in(fake_plex)
    old = tmp_path / "nexmix-0.1.0.db"
    _old_database(old, fake_plex.account_token)
    before = {table: _rows(old, table) for table in (*SAME_TABLES, "plex_playlists", "plex_connection")}
    # A backup made by 0.1.0 is a zip of the database, its secret and a manifest: made here from the old file.
    keep = tmp_path / "today.db"
    shutil.copyfile(get_settings().database_path, keep)
    _place(old)
    name = backups.create(kind=backups.MANUAL).name
    _place(keep)
    brief = backups.stage_restore(name)
    assert brief.database_ok and brief.schema == 8
    assert backups.apply_pending() is True
    database.init_db()
    return {"before": before, "state": state, "old": old}


def _now(table: str) -> list[tuple[Any, ...]]:
    return _rows(get_settings().database_path, table)


def test_everything_of_0_1_0_comes_over(restored: dict[str, Any], fake_plex: FakePlex) -> None:
    before = restored["before"]
    with closing(sqlite3.connect(get_settings().database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "plex_connection" not in names and "plex_playlists" not in names
    # The rows nexmix keeps for people come over as they were, to the last column.
    for table in SAME_TABLES:
        if table == "playlists":
            # One column more: the source server, the connected one for every playlist.
            assert [row[:-1] for row in _now(table)] == before[table], table
            assert {row[-1] for row in _now(table)} == {1}
        else:
            assert _now(table) == before[table], table

    with SessionLocal() as db:
        servers = db.scalars(select(MediaServer).order_by(MediaServer.id)).all()
        first, rest = servers[0], servers[1:]
        # The connection is server 1, its token still opens; the server the playlists came from rests, without one.
        assert (first.id, first.kind, first.active, first.machine_id) == (1, "plex", True, ATTIC)
        assert plex_link.token_of(first) == fake_plex.account_token
        assert [(one.machine_id, one.active, one.token_enc, one.url) for one in rest] == [(STUDIO, False, "", "")]
        studio = rest[0].id
        old_connection = before["plex_connection"][0]
        assert (first.serial, first.generation, first.section_key) == (old_connection[1], old_connection[11],
                                                                       old_connection[8])
        # Every entry's track is a thing of server 1 under its own key: the numbers did not move.
        entries = {row[2] for row in before["playlist_items"]}
        mapped = media.keys(db, 1, media.TRACK, entries)
        assert mapped == {track: str(track) for track in entries}
        # The copies on server 1 keep their row, key, common state and everything the sync knew.
        copies = {row.id: row for row in db.scalars(select(ServerPlaylist))}
        raw = {row[0]: row for row in _now("server_playlists")}
        for old in before["plex_playlists"]:
            (ident, playlist_id, machine, key, guid, smart, total, state, error, tries, due_at, creating_at,
             creating_title, base, writing, refused, seen, recheck, note, synced_at, gone, elsewhere) = old
            assert machine == ATTIC
            # The times and the name of a playlist being made, as SQLite holds them.
            assert (raw[ident][10], raw[ident][11], raw[ident][12], raw[ident][19]) == (due_at, creating_at,
                                                                                         creating_title, synced_at)
            row = copies.pop(ident)
            assert (row.playlist_id, row.server_id, row.ext_key, row.guid, row.smart, row.smart_total) == (
                playlist_id, 1, str(key) if key is not None else None, guid, bool(smart), total)
            assert (row.state, row.error, row.tries, row.recheck, row.gone_there, row.resume, row.settle) == (
                state, error, tries, bool(recheck), bool(gone), False, "")
            for column, value in (("base", base), ("writing", writing), ("refused", refused), ("seen", seen),
                                  ("note", note)):
                assert getattr(row, column) == (json.loads(value) if value is not None else None), column
            # What it left on the other server waits there, its tracks by numbers of that server.
            for name, copy in (json.loads(elsewhere) if elsewhere else {}).items():
                assert name == STUDIO
                left = next(one for one in copies.values() if one.playlist_id == playlist_id)
                copies.pop(left.id)
                assert (left.server_id, left.ext_key, left.guid, left.resume) == (studio, str(copy["key"]),
                                                                                   copy["guid"], True)
                tracks = [one[0] for one in left.base["items"]]
                keys = media.keys(db, studio, media.TRACK, tracks)
                assert [keys[track] for track in tracks] == [str(one[0]) for one in copy["base"]["items"]]
                assert [one[1:] for one in left.base["items"]] == [one[1:] for one in copy["base"]["items"]]
        assert copies == {}, "no copy came from nowhere"
        assert db.query(ServerItem).filter(ServerItem.server_id == studio).count() > 0


def test_the_first_sync_after_the_update_writes_nothing_to_plex(restored: dict[str, Any], fake_plex: FakePlex) -> None:
    before = restored["before"]
    server_sync.forget()
    for _round in range(3):
        fake_plex.tick(30)
        with SessionLocal() as db:
            server_sync.request_all(db)
        sync(fake_plex, look_at_plex=True)
    assert fake_plex.lists.writes == [], "nothing changed: nothing written"
    assert _now("playlist_items") == before["playlist_items"]
    assert [row[:-1] for row in _now("playlists")] == before["playlists"]
    with SessionLocal() as db:
        states = {(row.playlist_id, row.state) for row in db.scalars(select(ServerPlaylist)
                                                                     .where(ServerPlaylist.server_id == 1))}
    assert {state for _playlist, state in states} == {"synced"}
    # And a change in nexmix still goes to Plex, at the place it should.
    operator = _operator()
    with new_client(operator) as client:
        summer = next(card for card in client.get("/api/v1/playlists").json()["playlists"]
                      if card["name"] == "Sommerabend")
        assert summer["server"] == 1 and summer["plex"]["state"] == "synced"
        assert client.post(f"/api/v1/playlists/{summer['id']}/items", json={"tracks": [8002]}).status_code == 200
        sync(fake_plex)
        key = int(next(row for row in _now("server_playlists") if row[1] == summer["id"] and row[2] == 1)[3])
        assert fake_plex.lists.tracks(key)[-1] == 8002


def test_back_on_the_server_before_the_copies_there_are_taken_up_and_nothing_is_doubled(
        restored: dict[str, Any], fake_plex: FakePlex) -> None:
    """The step from 0.1.0 kept the copies on the server the playlists came from: connected to it again, its playlists
    are the same ones, not new ones beside them, and the track missing on the other server is there again."""
    state = restored["state"]
    server_sync.forget()
    playlists_before = _now("playlists")
    operator = _operator()
    with new_client(operator) as client:
        # Back to the first server, its library and its playlists as they were.
        fake_plex.lists.lists.clear()
        fake_plex.lists.lists.update(_lists(state["studio"]))
        from .fakeplex.library import made_up_library

        fake_plex.items.clear()
        fake_plex.items.update(made_up_library())
        fake_plex.servers[0].machine = STUDIO
        connect(client, fake_plex)
        for _round in range(3):
            fake_plex.tick(30)
            sync(fake_plex, look_at_plex=True)
        names = sorted(card["name"] for card in client.get("/api/v1/playlists").json()["playlists"])
        assert len(_now("playlists")) == len(playlists_before), "no playlist doubled"
        # The three that came from there are the same playlists there.
        studio_titles = sorted(one.title for one in fake_plex.lists.lists.values())
        assert len(studio_titles) == len(set(studio_titles)), "no second copy made in Plex"
        regen = next(card for card in client.get("/api/v1/playlists").json()["playlists"] if card["name"] == "Regentage")
        detail = client.get(f"/api/v1/playlists/{regen['id']}").json()
        assert [entry["plex_hint"] for entry in detail["items"]] == [None] * len(detail["items"])
        assert names


def _operator() -> Any:
    from app.models import Account

    with SessionLocal() as db:
        row = db.scalars(select(Account).where(Account.role == "operator")).one()
        db.expunge(row)
    return row


def test_a_copy_deleted_while_its_server_rested_sends_nothing_to_the_trash(restored: dict[str, Any],
                                                                             fake_plex: FakePlex) -> None:
    """Somebody deleted the copy on the first server while nexmix was on the other one: back there, the playlist is
    made anew, it does not go to the trash (the key is asked for in the list before it counts)."""
    state = restored["state"]
    server_sync.forget()
    studio = _lists(state["studio"])
    gone = next(key for key, one in studio.items() if one.title == "Regentage")
    del studio[gone]
    with new_client(_operator()) as client:
        fake_plex.lists.lists.clear()
        fake_plex.lists.lists.update(studio)
        from .fakeplex.library import made_up_library

        fake_plex.items.clear()
        fake_plex.items.update(made_up_library())
        fake_plex.servers[0].machine = STUDIO
        connect(client, fake_plex)
        for _round in range(3):
            fake_plex.tick(30)
            sync(fake_plex, look_at_plex=True)
        alive = {card["name"] for card in client.get("/api/v1/playlists").json()["playlists"]}
        trash = {card["name"] for card in client.get("/api/v1/playlists", params={"trash": True}).json()["playlists"]}
    assert "Regentage" in alive and "Regentage" not in trash
    made = [one for one in fake_plex.lists.lists.values() if one.title == "Regentage"]
    assert len(made) == 1 and made[0].key != gone


def test_moved_back_but_not_synced_yet_the_copies_there_are_taken_up(fake_plex: FakePlex, tmp_path: Path) -> None:
    """0.1.0 was connected to the first server again, and the playlists had not been synced with it since: each still
    names its copy on the other server. The step leaves those on the other server and takes up the ones the first
    server holds from before; the sync then finds them there and makes no second."""
    state = _stand_in(fake_plex)
    old = tmp_path / "nexmix-0.1.0.db"
    _old_database(old, fake_plex.account_token)
    with closing(sqlite3.connect(old)) as connection:
        connection.execute("UPDATE plex_connection SET machine_id = ?, server_name = 'Studio'", (STUDIO,))
        connection.commit()
    playlists_before = _rows(old, "playlists")
    _place(old)
    database.init_db()
    studio_keys = {copy["key"] for row in restored_rows(old) for name, copy in row.items() if name == STUDIO}
    with SessionLocal() as db:
        servers = db.scalars(select(MediaServer).order_by(MediaServer.id)).all()
        assert [(one.id, one.machine_id, one.active) for one in servers] == [(1, STUDIO, True), (2, ATTIC, False)]
        first = {row.playlist_id: row for row in db.scalars(select(ServerPlaylist).where(ServerPlaylist.server_id == 1))}
        taken = {int(row.ext_key) for row in first.values() if row.ext_key is not None}
        assert taken == studio_keys and all(row.resume for row in first.values() if row.ext_key is not None)
        assert {row.settle for row in first.values() if row.playlist_id is not None} == {"all"}
        attic = db.scalars(select(ServerPlaylist).where(ServerPlaylist.server_id == 2)).all()
        assert attic and all(row.resume and row.ext_key for row in attic)
    fake_plex.lists.lists.clear()
    fake_plex.lists.lists.update(_lists(state["studio"]))
    from .fakeplex.library import made_up_library

    fake_plex.items.clear()
    fake_plex.items.update(made_up_library())
    fake_plex.servers[0].machine = STUDIO
    server_sync.forget()
    from app.services import catalog, jobs

    with SessionLocal() as db:
        catalog.request_full(db, "first", 1)
    jobs.run_pending(lane="catalog")
    for _round in range(3):
        fake_plex.tick(30)
        sync(fake_plex, look_at_plex=True)
    assert len(_now("playlists")) == len(playlists_before), "no playlist doubled"
    titles = [one.title for one in fake_plex.lists.lists.values()]
    assert len(titles) == len(set(titles)), ("a second copy made in Plex", sorted(titles))
    assert {key for key, one in fake_plex.lists.lists.items() if one.title in ("Sommerabend", "Regentage")} <= (
        studio_keys)


def restored_rows(path: Path) -> list[dict[str, Any]]:
    """The copies each playlist of a database of 0.1.0 left on other servers (``elsewhere``)."""
    with closing(sqlite3.connect(path)) as connection:
        found = [row[0] for row in connection.execute("SELECT elsewhere FROM plex_playlists")]
    return [json.loads(value) for value in found if value]
