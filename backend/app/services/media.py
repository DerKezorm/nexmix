"""The media servers the playlists live on (``models.MediaServer``): which there are, which one a playlist takes its
tracks from, and the numbers nexmix gives their things.

**Servers.** A server is connected (``active``) or rests: let go of, or left for another one, it keeps its row without
its token, so that connected again it takes up the numbers of its things and the copies of the playlists on it. The
operator's Plex card speaks of the first active Plex server (``primary``).

**Numbers.** Every track, album and artist has a number of nexmix's own (``models.ServerItem``): a server's own key
(Plex's ``ratingKey``, another server's text) means something on that server only. ``numbers`` gives the numbers of keys
of one server, making the ones that are new; ``keys`` the other way round. A key that is a number (Plex's
``ratingKey``) gets itself as its number while no other thing has it, any other the next free one. A number is never
given twice: numbers are never taken back.

**Letting go.** ``drop_catalog`` takes a server's catalogue away and counts up its ``epoch``: a run or a sync that began
before stops at its next request instead of asking on with a token or a library that is no longer wanted.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.orm import Session

from ..models import (
    AlbumTag,
    ArtistTag,
    CatalogAlbum,
    CatalogArtist,
    CatalogTag,
    CatalogTrack,
    Job,
    MediaServer,
    Playlist,
    ServerItem,
    SyncRun,
    TrackTag,
)

TRACK, ALBUM, ARTIST = "track", "album", "artist"
#: Every number nexmix gives lies below this, and the API takes none above it: a key that is a larger number does not
#: keep itself as its number (the numbers after it would all lie beyond), it gets the next free one below.
NUMBER_LIMIT = 10**12
#: Keys in one ``IN (...)``: well below SQLite's limit of bound values.
_CHUNK = 500


# --- Servers ---------------------------------------------------------------------------------------------------------


def get(db: Session, server_id: int | None) -> MediaServer | None:
    if server_id is None:
        return None
    # Read afresh: another request may have let the server go since this session first saw it.
    return db.get(MediaServer, int(server_id), populate_existing=True)


def every(db: Session) -> list[MediaServer]:
    return list(db.scalars(select(MediaServer).order_by(MediaServer.id)))


def active(db: Session) -> list[MediaServer]:
    return list(db.scalars(select(MediaServer).where(MediaServer.active.is_(True)).order_by(MediaServer.id)))


def ready(db: Session) -> list[MediaServer]:
    """The active servers with a library chosen and its catalogue there."""
    return [row for row in active(db) if row.section_key and row.generation]


def primary(db: Session, kind: str | None = None) -> MediaServer | None:
    """The first active server (of a kind): the one the Plex card shows, and the source of new playlists."""
    query = select(MediaServer).where(MediaServer.active.is_(True))
    if kind is not None:
        query = query.where(MediaServer.kind == kind)
    return db.scalars(query.order_by(MediaServer.id).limit(1)).first()


def by_machine(db: Session, kind: str, machine: str) -> MediaServer | None:
    if not machine:
        return None
    return db.scalars(select(MediaServer).where(MediaServer.kind == kind, MediaServer.machine_id == machine)
                      .order_by(MediaServer.id).limit(1)).first()


def source_of(db: Session, playlist_id: int) -> int | None:
    found = db.scalar(select(Playlist.source_server_id).where(Playlist.id == playlist_id))
    return int(found) if found is not None else None


def generation_of(db: Session, server_id: int | None) -> int:
    """The live generation of a server's catalogue (0: none yet, or no such server)."""
    if server_id is None:
        return 0
    return int(db.scalar(select(MediaServer.generation).where(MediaServer.id == server_id)) or 0)


def view(row: MediaServer) -> dict[str, object]:
    """A server as every signed-in person may see it: what it is called and how it stands, never where it is or how
    nexmix signs in there."""
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "active": bool(row.active),
        "library": row.section_title if row.section_key else None,
        "ready": bool(row.active and row.section_key and row.generation),
    }


# --- Letting go ------------------------------------------------------------------------------------------------------

_epochs: dict[int, int] = {}
_epoch_lock = threading.Lock()


def epoch(server_id: int) -> int:
    """Counts up whenever the server's catalogue is let go of (another library, let go of, another server in its place):
    a run or a sync that began before stops at its next request. One process holds the servers (``--workers 1``); the
    serial of the row still decides at the switch."""
    return _epochs.get(int(server_id), 0)


def bump(server_id: int) -> None:
    with _epoch_lock:
        _epochs[int(server_id)] = _epochs.get(int(server_id), 0) + 1


def drop_catalog(db: Session, server_id: int) -> None:
    """A server's catalogue rows, its waiting catalogue jobs and the record of its runs; the covers kept on disk go too.
    A run or sync under way for it stops (``epoch``). Its numbers stay: connected again, its things keep them."""
    bump(server_id)
    for model in (TrackTag, AlbumTag, ArtistTag, CatalogTag, CatalogTrack, CatalogAlbum, CatalogArtist):
        db.execute(delete(model).where(model.server_id == server_id))
    db.execute(text("DELETE FROM track_search WHERE server_id = :s"), {"s": server_id})
    db.execute(delete(Job).where(Job.kind.in_(("catalog_full", "catalog_part")), Job.state == "queued",
                                 Job.ref == server_id))
    db.execute(delete(SyncRun).where(SyncRun.state != "running", SyncRun.server_id == server_id))
    from . import covers

    covers.forget_all()


# --- Numbers ---------------------------------------------------------------------------------------------------------


_IDS_OF = text(
    "SELECT ext_key, id FROM server_items WHERE server_id = :s AND kind = :k AND ext_key IN :keys"
).bindparams(bindparam("keys", expanding=True))
_KEYS_OF = text(
    "SELECT id, ext_key FROM server_items WHERE server_id = :s AND kind = :k AND id IN :ids"
).bindparams(bindparam("ids", expanding=True))


def _chunks(values: list[object]) -> Iterable[list[object]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start:start + _CHUNK]


def known_numbers(db: Session, server_id: int, kind: str, keys: Iterable[str]) -> dict[str, int]:
    """The numbers of the keys that have one; reads only."""
    wanted = sorted({str(key) for key in keys if str(key)})
    found: dict[str, int] = {}
    for part in _chunks(list(wanted)):
        for key, ident in db.execute(_IDS_OF, {"s": server_id, "k": kind, "keys": part}):
            found[str(key)] = int(ident)
    return found


def numbers(db: Session, server_id: int, kind: str, keys: Iterable[str]) -> dict[str, int]:
    """The numbers of a server's keys, the new ones made now, in the session's transaction (it writes: the caller
    commits). Two asking at once for the same new key get the same number (the unique index decides)."""
    wanted = sorted({str(key) for key in keys if str(key)})
    found = known_numbers(db, server_id, kind, wanted)
    missing = [key for key in wanted if key not in found]
    if missing:
        # A key that is a number below the limit keeps it as its number while no other thing has it (Plex's
        # ``ratingKey``s stay what they were); any other gets the next free one.
        plain = [key for key in missing if key.isdigit() and 0 < int(key) < NUMBER_LIMIT]
        if plain:
            db.execute(text("INSERT OR IGNORE INTO server_items (id, server_id, kind, ext_key) "
                            "VALUES (:id, :s, :k, :key)"),
                       [{"id": int(key), "s": server_id, "k": kind, "key": key} for key in plain])
        db.execute(text("INSERT OR IGNORE INTO server_items (server_id, kind, ext_key) VALUES (:s, :k, :key)"),
                   [{"s": server_id, "k": kind, "key": key} for key in missing])
        made = known_numbers(db, server_id, kind, missing)
        beyond = sorted(key for key, ident in made.items() if ident >= NUMBER_LIMIT)
        if beyond:
            made.update(_below_limit(db, server_id, kind, beyond))
        found.update(made)
    return found


def _below_limit(db: Session, server_id: int, kind: str, beyond: list[str]) -> dict[str, int]:
    """Keys that got a number at or above the limit (the one before kept a number just below it): each gets the
    largest free number below it instead. Numbers are never taken back, so a free one was never given."""
    db.execute(text("DELETE FROM server_items WHERE server_id = :s AND kind = :k AND ext_key IN :keys")
               .bindparams(bindparam("keys", expanding=True)), {"s": server_id, "k": kind, "keys": beyond})
    found: dict[str, int] = {}
    for key in beyond:
        free = db.execute(text(
            "SELECT id - 1 FROM server_items WHERE id > 1 AND id <= :limit AND id - 1 NOT IN (SELECT id FROM "
            "server_items) ORDER BY id DESC LIMIT 1"), {"limit": NUMBER_LIMIT}).scalar()
        if free is None:
            raise RuntimeError("No number left below the limit.")
        db.execute(text("INSERT INTO server_items (id, server_id, kind, ext_key) VALUES (:id, :s, :k, :key)"),
                   {"id": int(free), "s": server_id, "k": kind, "key": key})
        found[key] = int(free)
    return found


def keys(db: Session, server_id: int, kind: str, ids: Iterable[int]) -> dict[int, str]:
    """The server's keys of numbers; a number that is not one of this server's things of the kind is left out."""
    wanted = sorted({int(one) for one in ids})
    found: dict[int, str] = {}
    for part in _chunks(list(wanted)):
        for ident, key in db.execute(_KEYS_OF, {"s": server_id, "k": kind, "ids": part}):
            found[int(ident)] = str(key)
    return found


def all_numbers(db: Session, server_id: int, kind: str) -> dict[str, int]:
    """Every number of a server's things of a kind, by key: what a whole run of the catalogue starts from."""
    return {str(key): int(ident) for key, ident in db.execute(
        select(ServerItem.ext_key, ServerItem.id).where(ServerItem.server_id == server_id, ServerItem.kind == kind))}


class Numbers:
    """A catalogue run's view of the numbers of one server: everything read once, new keys numbered as they come, in the
    run's own transaction."""

    def __init__(self, db: Session, server_id: int) -> None:
        self.db = db
        self.server_id = server_id
        self.ids: dict[str, dict[str, int]] = {kind: all_numbers(db, server_id, kind)
                                               for kind in (TRACK, ALBUM, ARTIST)}
        self.back: dict[str, dict[int, str]] = {kind: {ident: key for key, ident in found.items()}
                                                for kind, found in self.ids.items()}

    def ensure(self, kind: str, keys_wanted: Iterable[object]) -> None:
        missing = {str(key) for key in keys_wanted if key is not None and str(key)} - set(self.ids[kind])
        if missing:
            made = numbers(self.db, self.server_id, kind, missing)
            self.ids[kind].update(made)
            self.back[kind].update({ident: key for key, ident in made.items()})

    def of(self, kind: str, key: object) -> int | None:
        """The number of a key (made when new); None for no key."""
        if key is None or not str(key) or str(key) == "0":
            return None
        found = self.ids[kind].get(str(key))
        if found is None:
            self.ensure(kind, [key])
            found = self.ids[kind][str(key)]
        return found

    def key(self, kind: str, ident: int) -> str | None:
        return self.back[kind].get(int(ident))
