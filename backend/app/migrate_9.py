"""Schema 9: from the one Plex connection to media servers.

What was there (schema 8): ``plex_connection`` (at most one row), the catalogue keyed by Plex's ``ratingKey``, and per
playlist one row of ``plex_playlists`` with its key in Plex, the common state of the sync (``base``), the write under
way, and in ``elsewhere`` the copies the playlist left on servers connected before.

What it becomes:

* ``media_servers``: the connected Plex is server 1, its token copied as it is (sealed with the same secret and the same
  context ``plex``, so nothing is opened or sealed anew here). Without a connection, the server most rows were synced
  with last is server 1, not active and without a token, so that connecting it again takes everything up as before.
  Every other server a playlist lay on gets a row of its own, not active, without a token.
* ``server_items``: every number the database holds for a thing of server 1 (catalogue, entries, common states, open
  writes) keeps its number: ``id = ratingKey``. That is what nexmix took the numbers for until now, so every entry,
  every guest's entry, every common state and every hidden suggestion means what it meant. The common states of copies
  on other servers get numbers of their own for those servers' keys (their ``ratingKey`` meant another track there).
* The catalogue's tables carry the server in their keys; the search index names the server of each row.
* ``server_playlists``: the row of a playlist synced with server 1 stays as it was, under its id (a job waiting for it
  still finds it). A row synced with another server (it was about to move) leaves that copy on its server, to be taken
  up again there, and the playlist starts on server 1 as the step to another server always did: its entries are found
  there first, and the copy server 1 holds from before is taken up when Plex still lists it. Each copy of ``elsewhere``
  becomes a row on its server, taken up the same way, and smart when the playlist is. Nothing is written to Plex by
  the step itself; the sync after it finds both sides equal and writes nothing.
* ``playlists.source_server_id``: server 1 for every playlist (none without a server).

The step runs in one transaction with the foreign keys off (``rebuilds_tables``): it breaks off whole or not at all.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC
from typing import Any

from sqlalchemy import Connection

from .services.media import NUMBER_LIMIT

#: The tables of schema 9 that are new.
NEW_TABLES = (
    (
        "CREATE TABLE media_servers ( id INTEGER NOT NULL, kind VARCHAR(16) NOT NULL, active BOOLEAN NOT NULL, serial "
        "INTEGER NOT NULL, via VARCHAR(8) NOT NULL, name VARCHAR(200) NOT NULL, machine_id VARCHAR(100) NOT NULL, "
        "owned BOOLEAN NOT NULL, url VARCHAR(500) NOT NULL, token_enc TEXT NOT NULL, section_key VARCHAR(16) NOT NULL, "
        "section_title VARCHAR(200) NOT NULL, connected_at DATETIME NOT NULL, generation INTEGER NOT NULL, synced_at "
        "DATETIME, full_at DATETIME, marks JSON, analysis_enabled BOOLEAN, analysis_sampled INTEGER NOT NULL, "
        "analysis_found INTEGER NOT NULL, analysis_at DATETIME, PRIMARY KEY (id) )"
    ),
    (
        "CREATE TABLE server_items ( id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL, kind "
        "VARCHAR(8) NOT NULL, ext_key VARCHAR(64) NOT NULL, CONSTRAINT uq_server_items_key UNIQUE (server_id, kind, "
        "ext_key), FOREIGN KEY(server_id) REFERENCES media_servers (id) ON DELETE CASCADE )"
    ),
    (
        "CREATE TABLE server_playlists ( id INTEGER NOT NULL, playlist_id INTEGER, server_id INTEGER NOT NULL, ext_key "
        "VARCHAR(64), guid VARCHAR(200) NOT NULL, smart BOOLEAN NOT NULL, smart_total INTEGER, state VARCHAR(8) NOT "
        "NULL, error VARCHAR(40) NOT NULL, tries INTEGER NOT NULL, due_at DATETIME, creating_at DATETIME, "
        "creating_title VARCHAR(200) DEFAULT '' NOT NULL, base JSON, writing JSON, refused JSON, seen JSON, recheck "
        "BOOLEAN NOT NULL, note JSON, synced_at DATETIME, gone_there BOOLEAN NOT NULL, resume BOOLEAN NOT NULL, settle "
        "VARCHAR(8) DEFAULT '' NOT NULL, PRIMARY KEY (id), FOREIGN KEY(playlist_id) REFERENCES playlists (id) ON "
        "DELETE SET NULL, FOREIGN KEY(server_id) REFERENCES media_servers (id) ON DELETE CASCADE )"
    ),
    "CREATE INDEX ix_server_playlists_due ON server_playlists (due_at)",
    "CREATE UNIQUE INDEX uq_server_playlists_key ON server_playlists (server_id, ext_key) WHERE ext_key IS NOT NULL",
    (
        "CREATE UNIQUE INDEX uq_server_playlists_playlist ON server_playlists (playlist_id, server_id) "
        "WHERE playlist_id IS NOT NULL"
    ),
)

#: The catalogue's tables as schema 9 has them, with the columns copied from schema 8 (the server goes in front).
CATALOGUE = {
    "artists": (
        ("CREATE TABLE artists ( server_id INTEGER NOT NULL, generation INTEGER NOT NULL, id INTEGER NOT NULL, title "
         "VARCHAR(500) NOT NULL, thumb VARCHAR(300) NOT NULL, added_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
         "PRIMARY KEY (server_id, generation, id) )"),
        "generation, id, title, thumb, added_at, updated_at",
    ),
    "albums": (
        ("CREATE TABLE albums ( server_id INTEGER NOT NULL, generation INTEGER NOT NULL, id INTEGER NOT NULL, "
         "artist_id INTEGER NOT NULL, title VARCHAR(500) NOT NULL, year INTEGER, thumb VARCHAR(300) NOT NULL, "
         "added_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY (server_id, generation, id) )"),
        "generation, id, artist_id, title, year, thumb, added_at, updated_at",
    ),
    "tracks": (
        ("CREATE TABLE tracks ( server_id INTEGER NOT NULL, generation INTEGER NOT NULL, id INTEGER NOT NULL, "
         "album_id INTEGER NOT NULL, artist_id INTEGER NOT NULL, title VARCHAR(500) NOT NULL, artist VARCHAR(500) NOT "
         "NULL, duration INTEGER NOT NULL, rating_count INTEGER NOT NULL, view_count INTEGER NOT NULL, year INTEGER, "
         "disc INTEGER, number INTEGER, added_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, song_key VARCHAR(700) "
         "NOT NULL, variant BOOLEAN NOT NULL, shown BOOLEAN NOT NULL, original_year INTEGER, guid VARCHAR(200) "
         "DEFAULT '' NOT NULL, PRIMARY KEY (server_id, generation, id) )"),
        ("generation, id, album_id, artist_id, title, artist, duration, rating_count, view_count, year, disc, number, "
         "added_at, updated_at, song_key, variant, shown, original_year, guid"),
    ),
    "tags": (
        ("CREATE TABLE tags ( server_id INTEGER NOT NULL, generation INTEGER NOT NULL, id INTEGER NOT NULL, kind "
         "VARCHAR(8) NOT NULL, name VARCHAR(200) NOT NULL, PRIMARY KEY (server_id, generation, id), CONSTRAINT "
         "uq_tags_name UNIQUE (server_id, generation, kind, name) )"),
        "generation, id, kind, name",
    ),
    "track_tags": (
        ("CREATE TABLE track_tags ( server_id INTEGER NOT NULL, generation INTEGER NOT NULL, item_id INTEGER NOT "
         "NULL, tag_id INTEGER NOT NULL, PRIMARY KEY (server_id, generation, item_id, tag_id) )"),
        "generation, item_id, tag_id",
    ),
    "album_tags": (
        ("CREATE TABLE album_tags ( server_id INTEGER NOT NULL, generation INTEGER NOT NULL, item_id INTEGER NOT "
         "NULL, tag_id INTEGER NOT NULL, PRIMARY KEY (server_id, generation, item_id, tag_id) )"),
        "generation, item_id, tag_id",
    ),
    "artist_tags": (
        ("CREATE TABLE artist_tags ( server_id INTEGER NOT NULL, generation INTEGER NOT NULL, item_id INTEGER NOT "
         "NULL, tag_id INTEGER NOT NULL, PRIMARY KEY (server_id, generation, item_id, tag_id) )"),
        "generation, item_id, tag_id",
    ),
}
CATALOGUE_INDEXES = (
    "CREATE INDEX ix_tracks_album ON tracks (server_id, generation, album_id)",
    "CREATE INDEX ix_tracks_song ON tracks (server_id, generation, song_key)",
)
SEARCH = "CREATE VIRTUAL TABLE track_search USING fts5(text, server_id UNINDEXED, tokenize = 'trigram')"

#: The columns of ``plex_playlists`` that go over as they are to a row synced with server 1.
SAME = ("guid", "smart", "smart_total", "state", "error", "tries", "due_at", "creating_at", "creating_title", "base",
        "writing", "refused", "seen", "recheck", "note", "synced_at")


def _moment() -> str:
    """Now, as SQLAlchemy writes a time into SQLite (UTC, without the zone)."""
    from . import clock

    return clock.now().astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def _rows(connection: Connection, sql: str, values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.exec_driver_sql(sql, values or {}).mappings()]


def _json(value: Any) -> Any:
    if value is None or isinstance(value, dict | list):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number < NUMBER_LIMIT else None


def _tracks_of_state(row: dict[str, Any]) -> set[int]:
    """Every track number a row's states name: the common state, the open write, the refusals, the note."""
    found: set[int] = set()
    base, writing = _json(row.get("base")), _json(row.get("writing"))
    for state in (base, writing):
        if isinstance(state, dict):
            for part in ("items", "remove"):
                for one in state.get(part) or []:
                    if isinstance(one, list) and one and _number(one[0]) is not None:
                        found.add(int(one[0]))
            for part in ("add", "order"):
                found.update(int(one) for one in state.get(part) or [] if _number(one) is not None)
    refused = _json(row.get("refused"))
    if isinstance(refused, dict):
        found.update(int(key) for key in refused if _number(key) is not None)
    note = _json(row.get("note"))
    if isinstance(note, dict):
        found.update(int(one) for one in note.get("order") or [] if _number(one) is not None)
    return found


class _Servers:
    """The rows of ``media_servers`` made by the step, by the server's machine id."""

    def __init__(self, connection: Connection, now: str) -> None:
        self.connection = connection
        self.now = now
        self.by_machine: dict[str, int] = {}

    def keep(self, row: dict[str, Any]) -> int:
        """Server 1 from the connection there was."""
        self.connection.exec_driver_sql(
            "INSERT INTO media_servers (id, kind, active, serial, via, name, machine_id, owned, url, token_enc, "
            "section_key, section_title, connected_at, generation, synced_at, full_at, marks, analysis_enabled, "
            "analysis_sampled, analysis_found, analysis_at) VALUES (1, 'plex', 1, :serial, :via, :server_name, "
            ":machine_id, :owned, :url, :token_enc, :section_key, :section_title, :connected_at, :generation, "
            ":synced_at, :full_at, :marks, :analysis_enabled, :analysis_sampled, :analysis_found, :analysis_at)", row)
        self.by_machine[str(row["machine_id"] or "")] = 1
        return 1

    def resting(self, machine: str, ident: int | None = None) -> int:
        """A server a playlist lay on, not connected now: no token, no catalogue."""
        found = self.by_machine.get(machine)
        if found is not None:
            return found
        values = {"machine": machine, "now": self.now}
        if ident is None:
            ident = int(self.connection.exec_driver_sql("SELECT coalesce(max(id), 0) + 1 FROM media_servers")
                        .scalar() or 1)
        values["id"] = ident
        self.connection.exec_driver_sql(
            "INSERT INTO media_servers (id, kind, active, serial, via, name, machine_id, owned, url, token_enc, "
            "section_key, section_title, connected_at, generation, marks, analysis_sampled, analysis_found) VALUES "
            "(:id, 'plex', 0, 1, '', '', :machine, 1, '', '', '', '', :now, 0, '{}', 0, 0)", values)
        self.by_machine[machine] = ident
        return ident


class _Numbers:
    """``server_items`` as the step fills it."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def keep(self, server: int, kind: str, numbers: set[int]) -> None:
        """Numbers of the first server keep themselves: ``id = ratingKey``. A number the step gave to another kind of
        thing already (only an entry of a server connected long ago can name one) is left without a mapping: nexmix
        never sends it to Plex as something it is not."""
        rows = [{"id": number, "server": server, "kind": kind, "key": str(number)} for number in sorted(numbers)]
        for start in range(0, len(rows), 2000):
            self.connection.exec_driver_sql(
                "INSERT OR IGNORE INTO server_items (id, server_id, kind, ext_key) VALUES (:id, :server, :kind, :key)",
                rows[start:start + 2000])

    def given(self, server: int, key: int) -> int:
        """The number of a track of another server, by its key there: the key itself while no thing of server 1 has
        it (they were all numbered first), else a new one."""
        values = {"server": server, "key": str(key), "id": int(key)}
        # As every server's numbers are given (``media.numbers``): its key, while no other thing has that number.
        self.connection.exec_driver_sql(
            "INSERT OR IGNORE INTO server_items (id, server_id, kind, ext_key) VALUES (:id, :server, 'track', :key)",
            values)
        self.connection.exec_driver_sql(
            "INSERT OR IGNORE INTO server_items (server_id, kind, ext_key) VALUES (:server, 'track', :key)", values)
        return int(self.connection.exec_driver_sql(
            "SELECT id FROM server_items WHERE server_id = :server AND kind = 'track' AND ext_key = :key", values
        ).scalar_one())


def _translated(base: Any, numbers: _Numbers, server: int) -> Any:
    """A common state of a copy on another server, its tracks named by their numbers for that server."""
    if not isinstance(base, dict):
        return None
    items = []
    for one in base.get("items") or []:
        if isinstance(one, list) and one and _number(one[0]) is not None:
            items.append([numbers.given(server, int(one[0])), *one[1:]])
    return {"title": base.get("title") if isinstance(base.get("title"), str) else "", "items": items}


def _rebuild_catalogue(connection: Connection, server: int | None) -> None:
    """The catalogue's tables with the server in front; their rows go to ``server`` (none: they go, there is no
    server they could belong to)."""
    for name, (create, columns) in CATALOGUE.items():
        connection.exec_driver_sql(create.replace(f"CREATE TABLE {name} ", f"CREATE TABLE {name}_9 ", 1))
        if server is not None:
            connection.exec_driver_sql(
                f"INSERT INTO {name}_9 (server_id, {columns}) SELECT {int(server)}, {columns} FROM {name}")  # noqa: S608
        connection.exec_driver_sql(f"DROP TABLE {name}")
        connection.exec_driver_sql(f"ALTER TABLE {name}_9 RENAME TO {name}")
    for statement in CATALOGUE_INDEXES:
        connection.exec_driver_sql(statement)
    kept = _rows(connection, "SELECT rowid AS id, text FROM track_search") if server is not None else []
    connection.exec_driver_sql("DROP TABLE track_search")
    connection.exec_driver_sql(SEARCH)
    rows = [{"id": row["id"], "text": row["text"], "server": server} for row in kept]
    for start in range(0, len(rows), 5000):
        connection.exec_driver_sql("INSERT INTO track_search (rowid, text, server_id) VALUES (:id, :text, :server)",
                                   rows[start:start + 5000])


def _first_numbers(connection: Connection, numbers: _Numbers, server: int, own_rows: list[dict[str, Any]]) -> None:
    """Every number the database holds for things of server 1 keeps itself: the catalogue first (it says what each
    number is), then the entries and the states of the copies on server 1."""
    for kind, table in (("artist", "artists"), ("album", "albums"), ("track", "tracks")):
        found = {int(row[0]) for row in connection.exec_driver_sql(f"SELECT DISTINCT id FROM {table}")}  # noqa: S608
        numbers.keep(server, kind, {number for number in found if _number(number) is not None})
    # Every entry, also one with the hint ``elsewhere`` (its track was on a server connected before): 0.1.0 read its
    # number as a ``ratingKey`` of the connected server too. The hint holds it back (it is never sent), and the entry
    # means what it meant; finding it on that server again is the sync's work once the server is back.
    entries = {int(row[0]) for row in connection.exec_driver_sql("SELECT DISTINCT track_id FROM playlist_items")}
    states: set[int] = set()
    for row in own_rows:
        states |= _tracks_of_state(row)
    numbers.keep(server, "track", {number for number in entries | states if _number(number) is not None})
    albums = {int(row[0]) for row in connection.exec_driver_sql("SELECT DISTINCT album_id FROM playlist_items")}
    albums |= {int(row[0]) for row in connection.exec_driver_sql("SELECT DISTINCT album_id FROM tracks")}
    numbers.keep(server, "album", {number for number in albums if _number(number) is not None})


def _insert_copy(connection: Connection, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    connection.exec_driver_sql(f"INSERT INTO server_playlists ({columns}) VALUES ({marks})", values)  # noqa: S608


def _plain(**values: Any) -> dict[str, Any]:
    """A row of ``server_playlists`` with the defaults of a copy nothing was done with yet."""
    row: dict[str, Any] = {"guid": "", "smart": 0, "smart_total": None, "state": "synced", "error": "", "tries": 0,
                           "due_at": None, "creating_at": None, "creating_title": "", "base": None, "writing": None,
                           "refused": None, "seen": None, "recheck": 0, "note": None, "synced_at": None,
                           "gone_there": 0, "resume": 0, "settle": ""}
    row.update(values)
    for name in ("base", "writing", "refused", "seen", "note"):
        if row[name] is not None and not isinstance(row[name], str):
            row[name] = json.dumps(row[name])
    return row


def _smartness(row: dict[str, Any]) -> dict[str, Any]:
    """Whether the playlist of a row is a smart one, for its copies on every server. 0.1.0 kept ``smart`` on the row
    when the playlist moved on (only its own server ever held it: a smart playlist was never made elsewhere), and its
    copies in ``elsewhere`` did not say; so each of them is what the row is. A smart copy is taken up again only as a
    smart one, and never made as an ordinary playlist on another server."""
    smart = bool(row.get("smart"))
    return {"smart": 1 if smart else 0, "smart_total": row.get("smart_total") if smart else None}


def run(connection: Connection) -> None:
    now = _moment()
    for statement in NEW_TABLES:
        connection.exec_driver_sql(statement)
    plex = _rows(connection, "SELECT * FROM plex_connection ORDER BY id LIMIT 1")
    rows = _rows(connection, "SELECT * FROM plex_playlists ORDER BY id")
    servers = _Servers(connection, now)
    numbers = _Numbers(connection)

    # Server 1: the connection there is; else the server most copies were synced with (connecting it again goes on
    # where it was); else none.
    first: int | None = None
    first_machine = ""
    if plex:
        first = servers.keep(plex[0])
        first_machine = str(plex[0]["machine_id"] or "")
    else:
        machines = Counter(str(row["machine"]) for row in rows if row["machine"])
        if machines:
            first_machine = max(machines, key=lambda machine: (machines[machine], machine))
            first = servers.resting(first_machine, ident=1)

    # A row belongs to server 1 when it names its machine, as 0.1.0 compared them: with a connection whose machine id is
    # empty, the rows of that empty id are its own (``mine`` in 0.1.0), never copies of another server.
    own = [row for row in rows if first is not None and row["machine"] == first_machine]
    # The copies server 1 holds from before, which the playlists take up again there: their states are of server 1 too.
    for row in rows:
        elsewhere = _json(row.get("elsewhere"))
        back = elsewhere.get(first_machine) if isinstance(elsewhere, dict) and first_machine else None
        if isinstance(back, dict):
            own.append({"base": back.get("base")})
    if first is not None:
        _first_numbers(connection, numbers, first, own)
    _rebuild_catalogue(connection, first if plex else None)

    connection.exec_driver_sql("ALTER TABLE sync_runs ADD COLUMN server_id INTEGER DEFAULT 0 NOT NULL")
    connection.exec_driver_sql("ALTER TABLE playlists ADD COLUMN source_server_id INTEGER")
    if first is not None:
        connection.exec_driver_sql("UPDATE sync_runs SET server_id = :s", {"s": first})
        connection.exec_driver_sql("UPDATE playlists SET source_server_id = :s", {"s": first})

    # The copies on server 1 keep their rows and ids; the rest are kept for later, on their servers.
    taken: set[tuple[int, str]] = set()
    held: set[tuple[int, int]] = set()
    later: list[dict[str, Any]] = []
    for row in rows:
        playlist = row["playlist_id"]
        key = _number(row["rating_key"])
        machine = str(row["machine"] or "")
        elsewhere = _json(row.get("elsewhere"))
        copies: dict[str, dict[str, Any]] = {}
        if isinstance(elsewhere, dict):
            copies = {str(name): copy for name, copy in elsewhere.items() if isinstance(copy, dict)}
        if first is None:
            # No server at all (never connected): nothing was made anywhere, there is nothing to keep.
            continue
        if machine == first_machine:
            values = {name: row[name] for name in SAME}
            values.update(id=row["id"], playlist_id=playlist, server_id=first,
                          ext_key=str(key) if key is not None else None, gone_there=row["gone_in_plex"])
            if key is not None:
                taken.add((first, str(key)))
            _insert_copy(connection, _plain(**values))
            if playlist is not None:
                held.add((playlist, first))
            copies.pop(first_machine, None)
        else:
            if machine and key is not None:
                # Its copy on the server it was synced with: kept there, taken up when that server is back.
                copies.setdefault(machine, {"key": key, "guid": row["guid"], "base": _json(row["base"])})
            back = copies.pop(first_machine, None) if machine else None
            if playlist is not None:
                values = {"id": row["id"], "playlist_id": playlist, "server_id": first, "state": "pending",
                          "due_at": now, "settle": "all" if machine else "since", **_smartness(row)}
                back_key = _number(back.get("key")) if isinstance(back, dict) else None
                if back_key is not None and (first, str(back_key)) not in taken:
                    base = _json(back.get("base"))
                    values.update(ext_key=str(back_key), guid=str(back.get("guid") or "")[:200],
                                  base=base if isinstance(base, dict) else None, resume=1)
                    taken.add((first, str(back_key)))
                _insert_copy(connection, _plain(**values))
                held.add((playlist, first))
        for name, copy in copies.items():
            later.append({"playlist": playlist, "machine": name, "copy": copy, "smartness": _smartness(row)})
    for one in later:
        copy = one["copy"]
        key = _number(copy.get("key"))
        if key is None or not one["machine"]:
            continue
        server = servers.resting(one["machine"])
        if (server, str(key)) in taken or (one["playlist"] is not None and (one["playlist"], server) in held):
            continue
        taken.add((server, str(key)))
        if one["playlist"] is not None:
            held.add((one["playlist"], server))
        _insert_copy(connection, _plain(playlist_id=one["playlist"], server_id=server, ext_key=str(key),
                                        guid=str(copy.get("guid") or "")[:200], resume=1,
                                        base=_translated(_json(copy.get("base")), numbers, server),
                                        **one["smartness"]))

    # Waiting work keeps its place: the catalogue's and the looks' jobs belong to server 1 now.
    if first is not None:
        connection.exec_driver_sql(
            "UPDATE jobs SET ref = :s WHERE kind IN ('catalog_full', 'catalog_part', 'plex_poll') "
            "AND state IN ('queued', 'running')", {"s": first})
    connection.exec_driver_sql("UPDATE jobs SET kind = 'server_round' WHERE kind = 'plex_round'")
    connection.exec_driver_sql("UPDATE jobs SET kind = 'server_poll' WHERE kind = 'plex_poll'")
    connection.exec_driver_sql("DROP TABLE plex_playlists")
    connection.exec_driver_sql("DROP TABLE plex_connection")
