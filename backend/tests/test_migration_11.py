"""The step to schema 11 (mirrors) on a database of schema 10 with playlists, entries and their copies on two servers:
everything stays, every copy is no mirror, the new tables are there and empty. A step that breaks off in the middle
leaves schema 10 as it was."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import db as database

from .test_schema import RECORDS, scratch, version_of  # noqa: F401

ROWS: list[tuple[str, dict[str, object]]] = [
    ("users", {"id": 1, "name": "otto", "display_name": "Otto", "role": "operator", "created_at": "2026-11-01 10:00:00",
               "sign_in": "password", "language": "de"}),
    ("media_servers", {"id": 1, "kind": "plex", "active": 1, "serial": 1, "name": "Studio", "section_key": "3",
                       "connected_at": "2026-11-01 10:00:00", "generation": 1}),
    ("media_servers", {"id": 2, "kind": "jellyfin", "active": 1, "serial": 1, "name": "Probe", "section_key": "lib",
                       "connected_at": "2026-11-01 10:00:00", "generation": 1, "user_id": "u1"}),
    ("playlists", {"id": 7, "owner_id": 1, "name": "Sommerfest", "version": 4, "created_at": "2026-11-01 10:00:00",
                   "updated_at": "2026-11-01 10:00:00", "source_server_id": 1}),
    ("playlist_items", {"id": 70, "playlist_id": 7, "track_id": 3001, "position": 0, "added_at": "2026-11-01 10:00:00",
                        "title": "Northern Lights"}),
    ("playlist_items", {"id": 71, "playlist_id": 7, "track_id": 3003, "position": 1, "added_at": "2026-11-01 10:00:00",
                        "title": "Glasshouse", "plex_hint": "missing"}),
    ("server_playlists", {"id": 1, "playlist_id": 7, "server_id": 1, "ext_key": "9001", "state": "synced",
                          "base": '{"title": "Sommerfest", "items": [[3001, 70, 1], [3003, 71, 2]]}'}),
    ("server_playlists", {"id": 2, "playlist_id": 7, "server_id": 2, "ext_key": "aa", "state": "synced",
                          "resume": 1}),
]


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    """A row with ``values``, every other column that may not be empty filled with nothing of its kind."""
    row = dict(values)
    for _cid, name, kind, notnull, default, _pk in connection.execute(f'PRAGMA table_info("{table}")'):
        if name not in row and notnull and default is None:
            row[name] = 0 if any(word in str(kind).upper() for word in ("INT", "BOOL")) else ""
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    connection.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(row.values()))  # noqa: S608


def _ten(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript((RECORDS / "v10.sql").read_text(encoding="utf-8"))
        for table, values in ROWS:
            _insert(connection, table, values)
        connection.execute("PRAGMA user_version = 10")
        connection.commit()


def test_a_database_of_schema_10_keeps_everything_and_has_no_mirror_yet(scratch: Path) -> None:  # noqa: F811
    _ten(scratch)
    database.init_db()
    assert version_of(scratch) == database.SCHEMA_VERSION >= 11
    with closing(sqlite3.connect(scratch)) as connection:
        rows = connection.execute("SELECT id, playlist_id, server_id, ext_key, base, resume, mirror FROM "
                                  "server_playlists ORDER BY id").fetchall()
        assert [(one[0], one[1], one[2], one[3], one[5], one[6]) for one in rows] == [
            (1, 7, 1, "9001", 0, 0), (2, 7, 2, "aa", 1, 0)]
        assert '"items": [[3001, 70, 1], [3003, 71, 2]]' in rows[0][4]
        assert connection.execute("SELECT id, track_id, position, plex_hint FROM playlist_items ORDER BY id")\
            .fetchall() == [(70, 3001, 0, ""), (71, 3003, 1, "missing")]
        assert connection.execute("SELECT name, version, source_server_id FROM playlists").fetchall() == [
            ("Sommerfest", 4, 1)]
        for table in ("mirror_items", "mirror_extras", "transfers"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_step_broken_off_in_the_middle_leaves_schema_10(scratch: Path,  # noqa: F811
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    _ten(scratch)
    whole = database._SCHEMA_11
    broken = (*whole[:3], "CREATE TABLE half_way (", *whole[3:])
    monkeypatch.setattr(database, "_SCHEMA_11", broken)
    with pytest.raises(Exception):  # noqa: B017
        database.init_db()
    assert version_of(scratch) == 10
    with closing(sqlite3.connect(scratch)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(server_playlists)")]
        assert "mirror" not in columns
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not tables & {"mirror_items", "mirror_extras", "transfers", "half_way"}
        assert connection.execute("SELECT count(*) FROM server_playlists").fetchone() == (2,)
    # Started again with the step whole, it goes through.
    monkeypatch.setattr(database, "_SCHEMA_11", whole)
    database.init_db()
    assert version_of(scratch) == database.SCHEMA_VERSION
