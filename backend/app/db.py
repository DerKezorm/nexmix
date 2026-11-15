"""SQLite connection, and the schema with its version number.

There is no Alembic. The database carries the version of its schema in SQLite's ``user_version``:

* A new database is made from the models (``create_all``) and gets ``SCHEMA_VERSION``.
* An older one is brought up step by step: ``MIGRATIONS[n]`` takes a database from version ``n - 1`` to ``n``. Before
  the first step a backup is made (the way back if the new version goes wrong); each step runs in a transaction of its
  own and sets the version only when it went through.
* A database from a newer nexmix than the running one is not touched: the start stops with a message.

Whoever changes a table in ``models.py`` raises ``SCHEMA_VERSION``, writes the step, and records the new schema in
``tests/schema/`` (``test_schema.py`` says how). Version 1 is the first schema; it has no step.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .config import get_settings
from .models import Base

logger = logging.getLogger("nexmix.db")

_settings = get_settings()
_settings.data_dir.mkdir(parents=True, exist_ok=True)

#: How long a write waits for another one to finish. Every writer keeps its transactions short; this is the margin
#: for a slow disk, not a wait anybody should see.
BUSY_SECONDS = 15

#: The version of the schema the models describe.
SCHEMA_VERSION = 10
#: The same, as it was when the module was loaded: only a database brought to it is compared with the models (the
#: tests of the schema bring records to older versions).
_MODELS_VERSION = SCHEMA_VERSION

#: Version 2: the Plex connection, the runs of the catalogue and the catalogue itself.
_SCHEMA_2 = (
    (
        "CREATE TABLE plex_connection ( id INTEGER NOT NULL, serial INTEGER NOT NULL, via VARCHAR(8) NOT "
        "NULL, server_name VARCHAR(200) NOT NULL, machine_id VARCHAR(100) NOT NULL, owned BOOLEAN NOT NULL, "
        "url VARCHAR(500) NOT NULL, token_enc TEXT NOT NULL, section_key VARCHAR(16) NOT NULL, section_title "
        "VARCHAR(200) NOT NULL, connected_at DATETIME NOT NULL, generation INTEGER NOT NULL, synced_at "
        "DATETIME, full_at DATETIME, marks JSON, analysis_enabled BOOLEAN, analysis_sampled INTEGER NOT NULL, "
        "analysis_found INTEGER NOT NULL, analysis_at DATETIME, PRIMARY KEY (id) )"
    ),
    (
        "CREATE TABLE sync_runs ( id INTEGER NOT NULL, kind VARCHAR(8) NOT NULL, reason VARCHAR(16) NOT NULL, "
        "state VARCHAR(8) NOT NULL, serial INTEGER NOT NULL, generation INTEGER NOT NULL, started_at DATETIME "
        "NOT NULL, finished_at DATETIME, artists INTEGER NOT NULL, albums INTEGER NOT NULL, tracks INTEGER "
        "NOT NULL, songs INTEGER NOT NULL, changed INTEGER NOT NULL, removed INTEGER NOT NULL, step "
        "VARCHAR(16) NOT NULL, progress INTEGER NOT NULL, error VARCHAR(40) NOT NULL, timings JSON, PRIMARY "
        "KEY (id) )"
    ),
    "CREATE INDEX ix_sync_runs_state ON sync_runs (state, id)",
    (
        "CREATE TABLE artists ( generation INTEGER NOT NULL, id INTEGER NOT NULL, title VARCHAR(500) NOT "
        "NULL, thumb VARCHAR(300) NOT NULL, added_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY "
        "KEY (generation, id) )"
    ),
    (
        "CREATE TABLE albums ( generation INTEGER NOT NULL, id INTEGER NOT NULL, artist_id INTEGER NOT NULL, "
        "title VARCHAR(500) NOT NULL, year INTEGER, thumb VARCHAR(300) NOT NULL, added_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL, PRIMARY KEY (generation, id) )"
    ),
    (
        "CREATE TABLE tracks ( generation INTEGER NOT NULL, id INTEGER NOT NULL, album_id INTEGER NOT NULL, "
        "artist_id INTEGER NOT NULL, title VARCHAR(500) NOT NULL, artist VARCHAR(500) NOT NULL, duration "
        "INTEGER NOT NULL, rating_count INTEGER NOT NULL, view_count INTEGER NOT NULL, year INTEGER, disc "
        "INTEGER, number INTEGER, added_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, song_key "
        "VARCHAR(700) NOT NULL, variant BOOLEAN NOT NULL, shown BOOLEAN NOT NULL, original_year INTEGER, "
        "PRIMARY KEY (generation, id) )"
    ),
    "CREATE INDEX ix_tracks_album ON tracks (generation, album_id)",
    "CREATE INDEX ix_tracks_song ON tracks (generation, song_key)",
    (
        "CREATE TABLE tags ( generation INTEGER NOT NULL, id INTEGER NOT NULL, kind VARCHAR(8) NOT NULL, name "
        "VARCHAR(200) NOT NULL, PRIMARY KEY (generation, id), CONSTRAINT uq_tags_name UNIQUE (generation, "
        "kind, name) )"
    ),
    (
        "CREATE TABLE track_tags ( generation INTEGER NOT NULL, item_id INTEGER NOT NULL, tag_id INTEGER NOT "
        "NULL, PRIMARY KEY (generation, item_id, tag_id) )"
    ),
    (
        "CREATE TABLE album_tags ( generation INTEGER NOT NULL, item_id INTEGER NOT NULL, tag_id INTEGER NOT "
        "NULL, PRIMARY KEY (generation, item_id, tag_id) )"
    ),
    (
        "CREATE TABLE artist_tags ( generation INTEGER NOT NULL, item_id INTEGER NOT NULL, tag_id INTEGER NOT "
        "NULL, PRIMARY KEY (generation, item_id, tag_id) )"
    ),
)


def _to_2(connection: Connection) -> None:
    for statement in _SCHEMA_2:
        connection.exec_driver_sql(statement)


#: Version 3: the playlists, their entries and shares, and the search index of the tracks.
_SCHEMA_3 = (
    (
        "CREATE TABLE playlists ( id INTEGER NOT NULL, owner_id INTEGER, name VARCHAR(200) NOT NULL, description "
        "TEXT NOT NULL, version INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
        "deleted_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE SET NULL )"
    ),
    "CREATE INDEX ix_playlists_owner_id ON playlists (owner_id)",
    "CREATE INDEX ix_playlists_deleted_at ON playlists (deleted_at)",
    (
        "CREATE TABLE playlist_items ( id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, track_id INTEGER NOT "
        "NULL, position INTEGER NOT NULL, added_by_user INTEGER, added_by_name VARCHAR(80) NOT NULL, added_at "
        "DATETIME NOT NULL, removed_at DATETIME, title VARCHAR(500) NOT NULL, artist VARCHAR(500) NOT NULL, album "
        "VARCHAR(500) NOT NULL, album_id INTEGER NOT NULL, year INTEGER, duration INTEGER NOT NULL, PRIMARY KEY "
        "(id), FOREIGN KEY(playlist_id) REFERENCES playlists (id) ON DELETE CASCADE, FOREIGN KEY(added_by_user) "
        "REFERENCES users (id) ON DELETE SET NULL )"
    ),
    "CREATE INDEX ix_playlist_items_order ON playlist_items (playlist_id, position)",
    (
        "CREATE UNIQUE INDEX uq_playlist_items_track ON playlist_items (playlist_id, track_id) "
        "WHERE removed_at IS NULL"
    ),
    (
        "CREATE TABLE playlist_shares ( playlist_id INTEGER NOT NULL, account_id INTEGER NOT NULL, access "
        "VARCHAR(8) NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (playlist_id, account_id), FOREIGN "
        "KEY(playlist_id) REFERENCES playlists (id) ON DELETE CASCADE, FOREIGN KEY(account_id) REFERENCES users (id) "
        "ON DELETE CASCADE )"
    ),
    "CREATE INDEX ix_playlist_shares_account_id ON playlist_shares (account_id)",
    # Filled by the next run of the catalogue (``services/catalog_view.reindex``); until then the search finds less.
    "CREATE VIRTUAL TABLE track_search USING fts5(text, tokenize = 'trigram')",
)


def _to_3(connection: Connection) -> None:
    for statement in _SCHEMA_3:
        connection.exec_driver_sql(statement)
    # The catalogue that is there already is indexed at once: its next run may be a quarter of an hour away. As the
    # tables stood then (one Plex connection, one live generation), not as ``catalog_view`` reads them today.
    from .services import search

    generation = connection.execute(text("SELECT generation FROM plex_connection LIMIT 1")).scalar() or 0
    if not generation:
        return
    albums = {row[0]: (row[1], row[2]) for row in connection.execute(
        text("SELECT id, title, artist_id FROM albums WHERE generation = :g"), {"g": generation})}
    artists = dict(connection.execute(text("SELECT id, title FROM artists WHERE generation = :g"),
                                      {"g": generation}).all())
    rows = []
    for track_id, title, album_id, own in connection.execute(
            text("SELECT id, title, album_id, artist FROM tracks WHERE generation = :g"), {"g": generation}):
        album_title, artist_id = albums.get(album_id, ("", 0))
        parts = (title, own, artists.get(artist_id, ""), album_title)
        rows.append({"id": int(track_id), "text": search.fold(" ".join(part for part in parts if part))})
    for start in range(0, len(rows), 5000):
        connection.execute(text("INSERT INTO track_search (rowid, text) VALUES (:id, :text)"),
                           rows[start:start + 5000])


#: Version 4: the copies of the playlists in Plex, entries put in through Plex, and the tracks' guids.
_SCHEMA_4 = (
    "ALTER TABLE playlist_items ADD COLUMN from_plex BOOLEAN DEFAULT 0 NOT NULL",
    "ALTER TABLE playlist_items ADD COLUMN guid VARCHAR(200) DEFAULT '' NOT NULL",
    "ALTER TABLE playlist_items ADD COLUMN plex_hint VARCHAR(16) DEFAULT '' NOT NULL",
    "ALTER TABLE tracks ADD COLUMN guid VARCHAR(200) DEFAULT '' NOT NULL",
    (
        "CREATE TABLE plex_playlists ( id INTEGER NOT NULL, playlist_id INTEGER, machine VARCHAR(100) NOT NULL, "
        "rating_key INTEGER, guid VARCHAR(200) NOT NULL, smart BOOLEAN NOT NULL, smart_total INTEGER, state "
        "VARCHAR(8) NOT NULL, error VARCHAR(40) NOT NULL, tries INTEGER NOT NULL, due_at DATETIME, creating_at "
        "DATETIME, creating_title VARCHAR(200) DEFAULT '' NOT NULL, base JSON, writing JSON, refused JSON, seen JSON, "
        "recheck BOOLEAN NOT NULL, note JSON, synced_at DATETIME, gone_in_plex BOOLEAN NOT NULL, "
        "PRIMARY KEY (id), FOREIGN KEY(playlist_id) REFERENCES playlists (id) ON DELETE SET NULL )"
    ),
    "CREATE UNIQUE INDEX uq_plex_playlists_key ON plex_playlists (rating_key) WHERE rating_key IS NOT NULL",
    "CREATE INDEX ix_plex_playlists_due ON plex_playlists (due_at)",
    "CREATE UNIQUE INDEX uq_plex_playlists_playlist ON plex_playlists (playlist_id) WHERE playlist_id IS NOT NULL",
    # Every playlist there is goes to Plex at the first chance.
    (
        "INSERT INTO plex_playlists (playlist_id, machine, guid, smart, state, error, tries, due_at, recheck, "
        "gone_in_plex) SELECT id, '', '', 0, 'pending', '', 0, CURRENT_TIMESTAMP, 0, 0 FROM playlists "
        "WHERE deleted_at IS NULL"
    ),
)


def _to_4(connection: Connection) -> None:
    for statement in _SCHEMA_4:
        connection.exec_driver_sql(statement)


#: Version 5: the songs each account asked not to be suggested any more.
_SCHEMA_5 = (
    (
        "CREATE TABLE hidden_suggestions ( id INTEGER NOT NULL, account_id INTEGER, guest_id INTEGER, song_key "
        "VARCHAR(700) NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id), FOREIGN KEY(account_id) REFERENCES "
        "users (id) ON DELETE CASCADE )"
    ),
    (
        "CREATE UNIQUE INDEX uq_hidden_suggestions_account ON hidden_suggestions (account_id, song_key) "
        "WHERE account_id IS NOT NULL"
    ),
    (
        "CREATE UNIQUE INDEX uq_hidden_suggestions_guest ON hidden_suggestions (guest_id, song_key) "
        "WHERE guest_id IS NOT NULL"
    ),
)


def _to_5(connection: Connection) -> None:
    for statement in _SCHEMA_5:
        connection.exec_driver_sql(statement)


#: Version 6: invitations for guests, the guests, and which entries they added.
_SCHEMA_6 = (
    (
        "CREATE TABLE guest_invites ( id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, created_by INTEGER, code_hash "
        "VARCHAR(64) NOT NULL, code_enc TEXT NOT NULL, may_add BOOLEAN NOT NULL, may_sort BOOLEAN NOT NULL, max_tracks "
        "INTEGER NOT NULL, hours INTEGER NOT NULL, preview BOOLEAN NOT NULL, created_at DATETIME NOT NULL, started_at "
        "DATETIME, expires_at DATETIME, ended_at DATETIME, PRIMARY KEY (id), FOREIGN KEY(playlist_id) REFERENCES "
        "playlists (id) ON DELETE CASCADE, FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL, UNIQUE "
        "(code_hash) )"
    ),
    "CREATE UNIQUE INDEX uq_guest_invites_open ON guest_invites (playlist_id) WHERE ended_at IS NULL",
    "CREATE INDEX ix_guest_invites_ended_at ON guest_invites (ended_at)",
    (
        "CREATE TABLE guests ( id INTEGER NOT NULL, invite_id INTEGER NOT NULL, name VARCHAR(40) NOT NULL, name_key "
        "VARCHAR(160) NOT NULL, token_hash VARCHAR(64) NOT NULL, joined_at DATETIME NOT NULL, removed_at DATETIME, "
        "PRIMARY KEY (id), CONSTRAINT uq_guests_name UNIQUE (invite_id, name_key), FOREIGN KEY(invite_id) REFERENCES "
        "guest_invites (id) ON DELETE CASCADE, UNIQUE (token_hash) )"
    ),
    "CREATE INDEX ix_guests_invite_id ON guests (invite_id)",
    (
        "CREATE TABLE guest_entries ( item_id INTEGER NOT NULL, guest_id INTEGER NOT NULL, PRIMARY KEY (item_id), "
        "FOREIGN KEY(item_id) REFERENCES playlist_items (id) ON DELETE CASCADE, FOREIGN KEY(guest_id) REFERENCES "
        "guests (id) ON DELETE CASCADE )"
    ),
    "CREATE INDEX ix_guest_entries_guest_id ON guest_entries (guest_id)",
)


def _to_6(connection: Connection) -> None:
    for statement in _SCHEMA_6:
        connection.exec_driver_sql(statement)


#: Version 7: what a playlist left on a server connected before, to take it up again there.
_SCHEMA_7 = ("ALTER TABLE plex_playlists ADD COLUMN elsewhere JSON",)


def _to_7(connection: Connection) -> None:
    for statement in _SCHEMA_7:
        connection.exec_driver_sql(statement)


#: Version 8: the playlists an account hid from its own view.
_SCHEMA_8 = (
    (
        "CREATE TABLE hidden_playlists ( account_id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, created_at "
        "DATETIME NOT NULL, PRIMARY KEY (account_id, playlist_id), FOREIGN KEY(account_id) REFERENCES users (id) ON "
        "DELETE CASCADE, FOREIGN KEY(playlist_id) REFERENCES playlists (id) ON DELETE CASCADE )"
    ),
    "CREATE INDEX ix_hidden_playlists_playlist_id ON hidden_playlists (playlist_id)",
)


def _to_8(connection: Connection) -> None:
    for statement in _SCHEMA_8:
        connection.exec_driver_sql(statement)


def _to_9(connection: Connection) -> None:
    from . import migrate_9

    migrate_9.run(connection)


# The catalogue's tables are made anew with the server in their keys: SQLite changes no primary key in place.
_to_9.rebuilds_tables = True  # type: ignore[attr-defined]


#: Version 10: Jellyfin. The user its playlists will belong to, and the MusicBrainz ids of a track.
_SCHEMA_10 = (
    "ALTER TABLE media_servers ADD COLUMN user_id VARCHAR(64) DEFAULT '' NOT NULL",
    "ALTER TABLE media_servers ADD COLUMN user_name VARCHAR(200) DEFAULT '' NOT NULL",
    "ALTER TABLE tracks ADD COLUMN mbid_track VARCHAR(36) DEFAULT '' NOT NULL",
    "ALTER TABLE tracks ADD COLUMN mbid_recording VARCHAR(36) DEFAULT '' NOT NULL",
)


def _to_10(connection: Connection) -> None:
    for statement in _SCHEMA_10:
        connection.exec_driver_sql(statement)


#: ``MIGRATIONS[n]`` brings a database from version ``n - 1`` to ``n``. Version 1 is the first schema; it has no step.
#: Later steps are written out as the tables stood then, not taken from the models, which move on.
MIGRATIONS: dict[int, Callable[[Connection], None]] = {2: _to_2, 3: _to_3, 4: _to_4, 5: _to_5, 6: _to_6, 7: _to_7,
                                                       8: _to_8, 9: _to_9, 10: _to_10}


# No pool with an upper bound: with the default pool the sixteenth concurrent request would block the event
# loop waiting for a connection. Opening a SQLite connection costs a fraction of a millisecond.
engine = create_engine(
    f"sqlite:///{_settings.database_path}",
    connect_args={"check_same_thread": False, "timeout": BUSY_SECONDS},
    poolclass=NullPool,
    # A failed statement names its values in the exception, and exceptions reach the log: never texts or hashes.
    hide_parameters=True,
)


def _wal(cursor: Any) -> None:
    """WAL mode, tried again while another connection holds the database: SQLite does not always wait by itself for
    a change of the journal mode."""
    deadline = time.monotonic() + BUSY_SECONDS
    while True:
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if ("locked" not in str(exc) and "busy" not in str(exc)) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


@event.listens_for(engine, "connect")
def _pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    # The wait first: switching to WAL needs the database to itself for a moment, and two first starts on an empty
    # folder asked at the same time (one of them broke off with "database is locked").
    cursor.execute(f"PRAGMA busy_timeout={BUSY_SECONDS * 1000}")
    _wal(cursor)
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # What is deleted is overwritten, not left in free pages: a deleted token hash or secret must be gone from the file.
    cursor.execute("PRAGMA secure_delete=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


class SchemaTooNew(RuntimeError):
    """The database was written by a newer nexmix; this one does not know its tables."""


def schema_version(connection: Connection) -> int:
    return int(connection.execute(text("PRAGMA user_version")).scalar() or 0)


def _set_version(connection: Connection, version: int) -> None:
    # PRAGMA takes no bound parameters; the value is an int from this module, never from outside.
    connection.execute(text(f"PRAGMA user_version = {int(version)}"))


def _schema_engine(url: Any = None) -> Engine:
    """An engine for changing the schema, on the same file. Python's sqlite3 opens no transaction before ``CREATE`` or
    ``ALTER`` and so commits each one at once; here every transaction begins with an explicit ``BEGIN``, so a step that
    breaks off leaves nothing behind (SQLAlchemy's recipe for pysqlite). Only for the schema: the everyday engine
    keeps the driver's way, which holds no read lock while a request only reads."""
    schema = create_engine(url or engine.url, poolclass=NullPool, connect_args={"timeout": BUSY_SECONDS})

    @event.listens_for(schema, "connect")
    def _no_driver_transactions(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.execute("PRAGMA secure_delete=ON")
        dbapi_connection.execute(f"PRAGMA busy_timeout={BUSY_SECONDS * 1000}")

    @event.listens_for(schema, "begin")
    def _begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return schema


def _has_tables(connection: Connection) -> bool:
    found = connection.execute(text("SELECT count(*) FROM sqlite_master WHERE type = 'table'")).scalar()
    return bool(found)


def init_db() -> None:
    """Makes a new database, or brings an older one up to ``SCHEMA_VERSION``."""
    schema = _schema_engine()
    try:
        _init(schema)
        if SCHEMA_VERSION == _MODELS_VERSION:
            _check_complete(schema)
    finally:
        schema.dispose()


def _init(schema: Engine, *, backup: bool = True) -> None:
    # Read under the write lock: of two processes starting at once, the second waits here and sees what the first did.
    with schema.begin() as connection:
        fresh = not _has_tables(connection)
        current = schema_version(connection)
        if fresh:
            # One transaction: a start that breaks off in between leaves no tables without a version behind.
            Base.metadata.create_all(connection)
            _set_version(connection, SCHEMA_VERSION)
    if fresh:
        logger.info("Database created schema=%s", SCHEMA_VERSION)
        return
    check_version(current)
    if current < SCHEMA_VERSION:
        if backup:
            # A schema change is the moment a backup is worth most: the way back if the new version goes wrong.
            from .services import backups

            backups.create(kind=backups.UPDATE, note=f"before schema {SCHEMA_VERSION}")
        for target in range(current + 1, SCHEMA_VERSION + 1):
            step = MIGRATIONS.get(target)
            if step is None:
                raise RuntimeError(f"No migration to schema {target}.")
            if _take_step(schema, target, step):
                logger.info("Database migrated schema=%s", target)


def _check_complete(schema: Engine) -> None:
    """Every table and column a database of today holds is there. A database whose number says one schema and whose
    tables say an older one (a version of nexmix in between that numbered a step it did not take) would start and fail
    at its first use; it stops the start here instead, with what is missing."""
    with schema.connect() as connection:
        missing: list[str] = []
        for name, columns in sorted(_reference()[0].items()):
            found = {row[1] for row in connection.exec_driver_sql(f'PRAGMA table_info("{name}")')}
            if not found:
                missing.append(name)
            else:
                missing.extend(f"{name}.{column}" for column in sorted(columns - found))
    if missing:
        raise NotOurs("The database lacks what this nexmix needs: " + ", ".join(missing[:12])
                      + (" ..." if len(missing) > 12 else "") + ". Restore a backup made with this version.")


def _take_step(schema: Engine, target: int, step: Callable[[Connection], None]) -> bool:
    """One step in a transaction of its own; False when another process took it meanwhile. A step that makes tables
    again (``rebuilds_tables``) runs with the foreign keys off, which SQLite lets change only outside a transaction: set
    on the connection before it begins, and back on afterwards (the connection is not kept, ``NullPool``)."""
    rebuilds = bool(getattr(step, "rebuilds_tables", False))
    with schema.connect() as connection:
        raw = connection.connection.dbapi_connection
        assert raw is not None
        if rebuilds:
            raw.execute("PRAGMA foreign_keys=OFF")
        try:
            with connection.begin():
                # Read again under the lock: another process may have taken this step since the first read. A step
                # that is not idempotent (an added column, moved rows) must never run twice.
                if schema_version(connection) >= target:
                    return False
                known = _broken_keys(connection) if rebuilds else set()
                step(connection)
                if rebuilds:
                    # With the foreign keys off nothing stopped the step from leaving a row whose parent is gone: one
                    # it left breaks the step off whole (the database stays as it was). What was broken before stays.
                    broken = _broken_keys(connection) - known
                    if broken:
                        tables = sorted({table for table, _row, _parent, _key in broken})
                        logger.error("Database migration broke off: rows without their parent schema=%s count=%s "
                                     "tables=%s", target, len(broken), ",".join(tables))
                        raise MigrationBroken(f"The step to schema {target} left {len(broken)} rows without their "
                                              f"parent in {', '.join(tables)}; the database is unchanged.")
                _set_version(connection, target)
        finally:
            if rebuilds:
                raw.execute("PRAGMA foreign_keys=ON")
    return True


def _broken_keys(connection: Connection) -> set[tuple[str, int, str, int]]:
    """The rows whose parent is gone (``PRAGMA foreign_key_check``): table, row, parent table, which key."""
    return {(str(table), int(row or 0), str(parent), int(key or 0))
            for table, row, parent, key in connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()}


class MigrationBroken(RuntimeError):
    """A step of the schema left rows whose parent is gone: it was rolled back."""


class NotOurs(RuntimeError):
    """A database that is not a nexmix database, or holds things nexmix never made."""


_REFERENCE: tuple[dict[str, frozenset[str]], dict[str, str]] | None = None


def _squeezed(sql: object) -> str:
    """A statement as SQLite kept it, without the differences that mean nothing (spaces, line breaks, case)."""
    return " ".join(str(sql or "").split()).lower()


def _reference() -> tuple[dict[str, frozenset[str]], dict[str, str]]:
    """What a database of today holds, made the way a new one is: ``(tables with their columns, the statements of
    its triggers, views and virtual tables by name)``. Made once."""
    global _REFERENCE
    if _REFERENCE is None:
        reference = create_engine("sqlite://", poolclass=NullPool)
        # A memory database is gone with its connection: made and read on one.
        with reference.connect() as connection:
            Base.metadata.create_all(connection)
            names = [row[0] for row in
                     connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")]
            tables = {
                name: frozenset(row[1] for row in connection.exec_driver_sql(f'PRAGMA table_info("{name}")'))
                for name in names if not name.startswith("sqlite_")
            }
            code = {
                row[0]: _squeezed(row[1]) for row in connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master WHERE type IN ('trigger', 'view') "
                    "OR (type = 'table' AND sql LIKE 'CREATE VIRTUAL%')")
            }
        reference.dispose()
        _REFERENCE = (tables, code)
    return _REFERENCE


def _foreign_objects(plain: sqlite3.Connection) -> list[str]:
    """What the database holds that code of nexmix did not make and that is more than data: triggers and views (they
    would run at every write of the app, and name anything), and virtual tables (a module of SQLite stands behind
    each one). One of ours by name counts only when its statement is ours, too: a trigger named like the one that keeps
    the search index, with another body, is the first thing somebody would try. Plain tables of data nexmix does not
    know are not in the list: nothing reads them."""
    _tables, own = _reference()
    query = "SELECT name, type, sql FROM sqlite_master WHERE type IN ('trigger', 'view', 'table')"
    found = []
    for name, kind, sql in plain.execute(query):
        if kind == "table" and not str(sql or "").lstrip().upper().startswith("CREATE VIRTUAL"):
            continue
        if own.get(name) != _squeezed(sql) and _squeezed(sql) not in _EARLIER.get(name, ()):
            found.append(name)
    return found


#: Statements of objects an older nexmix made, as their schema had them: a backup of that version holds them, and its
#: steps make them anew (schema 3 to 8: the search index without the server of each row).
_EARLIER: dict[str, frozenset[str]] = {
    "track_search": frozenset({_squeezed("CREATE VIRTUAL TABLE track_search USING fts5(text, tokenize = 'trigram')")}),
}


def rehearse(path: Path) -> None:
    """The start, run on a copy of a database that is about to be restored: the steps that would bring it up to
    ``SCHEMA_VERSION`` and then a look whether it holds every table and column nexmix needs. Raises when anything of
    it fails (a database that is not nexmix's, one with a step that cannot run on it); the file is changed, so only
    ever hand it a scratch copy.

    Triggers, views and virtual tables that nexmix does not make are refused before anything is done: a trigger would
    run at every write of the app, and the restore would give it the keys to everything.
    """
    uri = f"file:{path.as_posix()}?mode=ro"
    plain = sqlite3.connect(uri, uri=True)
    try:
        if _foreign_objects(plain):
            raise NotOurs("triggers, views or virtual tables that nexmix did not make")
        if plain.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] == 0:
            raise NotOurs("no tables")
    finally:
        plain.close()
    schema = _schema_engine(f"sqlite:///{path.as_posix()}")
    try:
        _init(schema, backup=False)
    finally:
        schema.dispose()
    plain = sqlite3.connect(uri, uri=True)
    try:
        for name, columns in _reference()[0].items():
            found = {row[1] for row in plain.execute(f'PRAGMA table_info("{name}")')}
            if not columns <= found:
                raise NotOurs("a table or a column is missing")
    finally:
        plain.close()


def check_version(version: int) -> None:
    """A schema this nexmix can open: made by nexmix (at least 1), and not by a newer one. ``SchemaTooNew`` else."""
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"The database has schema {version}, this nexmix knows up to {SCHEMA_VERSION}. "
            "Start the newer version again, or restore a backup made with this one."
        )
    if version < 1:
        raise SchemaTooNew("The database has no schema version. It was not made by nexmix.")
