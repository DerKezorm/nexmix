"""The step to schema 12 (Music Assistant) on a database of schema 11 with two servers, a playlist and its mirror:
everything stays, the servers have no providers and no end of a token yet. A step that breaks off in the middle leaves
schema 11 as it was."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import db as database

from .test_migration_11 import ROWS, _insert
from .test_schema import RECORDS, scratch, version_of  # noqa: F401


def _eleven(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript((RECORDS / "v11.sql").read_text(encoding="utf-8"))
        for table, values in ROWS:
            _insert(connection, table, values)
        connection.execute("UPDATE server_playlists SET mirror = 1 WHERE id = 2")
        connection.execute("PRAGMA user_version = 11")
        connection.commit()


def test_a_database_of_schema_11_keeps_everything(scratch: Path) -> None:  # noqa: F811
    _eleven(scratch)
    database.init_db()
    assert version_of(scratch) == database.SCHEMA_VERSION == 12
    with closing(sqlite3.connect(scratch)) as connection:
        assert connection.execute("SELECT id, kind, name, user_id, providers, token_expires_at FROM media_servers "
                                  "ORDER BY id").fetchall() == [(1, "plex", "Studio", "", None, None),
                                                                (2, "jellyfin", "Probe", "u1", None, None)]
        rows = connection.execute("SELECT id, playlist_id, server_id, ext_key, mirror FROM server_playlists "
                                  "ORDER BY id").fetchall()
        assert rows == [(1, 7, 1, "9001", 0), (2, 7, 2, "aa", 1)]
        assert connection.execute("SELECT id, track_id, plex_hint FROM playlist_items ORDER BY id").fetchall() == [
            (70, 3001, ""), (71, 3003, "missing")]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_step_broken_off_in_the_middle_leaves_schema_11(scratch: Path,  # noqa: F811
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    _eleven(scratch)
    whole = database._SCHEMA_12
    monkeypatch.setattr(database, "_SCHEMA_12", (whole[0], "ALTER TABLE nowhere ADD COLUMN x INTEGER", whole[1]))
    with pytest.raises(Exception):  # noqa: B017
        database.init_db()
    assert version_of(scratch) == 11
    with closing(sqlite3.connect(scratch)) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(media_servers)")]
        assert "providers" not in columns and "token_expires_at" not in columns
        assert connection.execute("SELECT count(*) FROM server_playlists").fetchone() == (2,)
