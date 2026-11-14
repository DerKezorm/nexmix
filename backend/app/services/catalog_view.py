"""The catalogue as the browser gets it: compact, packed, built once per state of the catalogue.

**The compact catalogue** (``GET /api/v1/catalog``) is a JSON object of lists, places instead of names:

* ``moods``, ``genres``, ``styles``: the names, sorted.
* ``artists``: ``[id, name, [moods], [styles]]``.
* ``albums``: ``[id, title, artist, year, cover, [genres], [styles], [moods], added]``; ``artist`` is a place in
  ``artists`` (-1: unknown), ``cover`` 1 when Plex has one, ``added`` Plex's time.
* ``tracks``: ``[id, title, album, own artist, seconds, listeners, plays, [moods], disc, number, song, original year]``;
  ``album`` a place in ``albums``, ``own artist`` empty where it is the album's, ``song`` the id of the version shown
  for the song (the track *is* that version when ``song`` equals its id), ``original year`` the earliest album year
  among the versions (0: none).

It is built when first asked for after the catalogue changed (and right after each run), kept in memory with its
packed form, and named by its content (``etag``): an unchanged catalogue after a partial run keeps its name, and the
browser's copy stays good (``304``).

**The search index** (``track_search``, a full-text table cut in threes) holds the live tracks' folded texts; the
server's search (``search_tracks``) takes from it the tracks worth looking at and then applies the same rules as the
browser (``search.py``).
"""

from __future__ import annotations

import ctypes
import gc
import gzip
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import (
    AlbumTag,
    ArtistTag,
    CatalogAlbum,
    CatalogArtist,
    CatalogTag,
    CatalogTrack,
    PlexConnection,
    TrackTag,
)
from . import search

logger = logging.getLogger("nexmix.catalog")

FORMAT = 1


@dataclass(frozen=True)
class View:
    """One state of the catalogue: what it was made from, the compact JSON (plain and packed), its name, the index."""

    stamp: tuple[int, str]
    body: bytes
    packed: bytes
    etag: str
    index: search.Index
    built_seconds: float
    tracks: int


_lock = threading.Lock()
_view: View | None = None


def forget() -> None:
    global _view
    with _lock:
        _view = None


def _stamp(db: Session) -> tuple[int, str] | None:
    """The live generation and the time of the last run, in one fixed state of the database (``catalog.pin``): every
    query of the session after it sees that state, whatever a run switches or writes meanwhile."""
    from . import catalog

    generation = catalog.pin(db)
    if not generation:
        return None
    synced = db.scalar(select(PlexConnection.synced_at).order_by(PlexConnection.id).limit(1))
    return generation, synced.isoformat() if synced else ""


def compact(db: Session, generation: int) -> dict[str, Any]:
    """The compact catalogue of one generation (see the top of this file)."""
    tags = {row.id: (row.kind, row.name) for row in db.execute(
        select(CatalogTag.id, CatalogTag.kind, CatalogTag.name).where(CatalogTag.generation == generation))}
    names: dict[str, list[str]] = {"mood": [], "genre": [], "style": []}
    for kind, name in tags.values():
        if kind in names:
            names[kind].append(name)
    for values in names.values():
        values.sort()
    place = {kind: {name: number for number, name in enumerate(values)} for kind, values in names.items()}
    tag_place = {tag_id: (kind, place[kind][name]) for tag_id, (kind, name) in tags.items() if kind in place}

    def links(model: type[Any]) -> dict[int, dict[str, list[int]]]:
        found: dict[int, dict[str, list[int]]] = {}
        for item, tag in db.execute(select(model.item_id, model.tag_id).where(model.generation == generation)):
            known = tag_place.get(tag)
            if known is not None:
                found.setdefault(item, {}).setdefault(known[0], []).append(known[1])
        for kinds in found.values():
            for values in kinds.values():
                values.sort()
        return found

    artist_tags, album_tags, track_tags = links(ArtistTag), links(AlbumTag), links(TrackTag)
    artists = db.execute(select(CatalogArtist.id, CatalogArtist.title).where(CatalogArtist.generation == generation)
                         .order_by(CatalogArtist.id)).all()
    artist_place = {row.id: number for number, row in enumerate(artists)}
    albums = db.execute(select(CatalogAlbum.id, CatalogAlbum.artist_id, CatalogAlbum.title, CatalogAlbum.year,
                               CatalogAlbum.thumb, CatalogAlbum.added_at)
                        .where(CatalogAlbum.generation == generation).order_by(CatalogAlbum.id)).all()
    album_place = {row.id: number for number, row in enumerate(albums)}
    tracks = db.execute(select(
        CatalogTrack.id, CatalogTrack.title, CatalogTrack.album_id, CatalogTrack.artist, CatalogTrack.duration,
        CatalogTrack.rating_count, CatalogTrack.view_count, CatalogTrack.disc, CatalogTrack.number,
        CatalogTrack.song_key, CatalogTrack.shown, CatalogTrack.original_year,
    ).where(CatalogTrack.generation == generation).order_by(CatalogTrack.id)).all()
    shown: dict[str, int] = {}
    for row in tracks:
        if row.shown and row.song_key not in shown:
            shown[row.song_key] = row.id
    for row in tracks:
        shown.setdefault(row.song_key, row.id)
    return {
        "format": FORMAT,
        "generation": generation,
        "moods": names["mood"],
        "genres": names["genre"],
        "styles": names["style"],
        "artists": [[row.id, row.title, artist_tags.get(row.id, {}).get("mood", []),
                     artist_tags.get(row.id, {}).get("style", [])] for row in artists],
        "albums": [[row.id, row.title, artist_place.get(row.artist_id, -1), row.year or 0, 1 if row.thumb else 0,
                    album_tags.get(row.id, {}).get("genre", []), album_tags.get(row.id, {}).get("style", []),
                    album_tags.get(row.id, {}).get("mood", []), row.added_at or 0] for row in albums],
        "tracks": [[row.id, row.title, album_place.get(row.album_id, -1), row.artist or "",
                    round((row.duration or 0) / 1000), row.rating_count or 0, row.view_count or 0,
                    track_tags.get(row.id, {}).get("mood", []), row.disc or 0, row.number or 0,
                    shown[row.song_key], row.original_year or 0] for row in tracks],
    }


def current(db: Session | None = None) -> View | None:
    """The view of the live catalogue, built now when the catalogue changed since; None before the first run. The
    generation is read once (``catalog.pin``), and everything else of it in the same read transaction."""
    global _view
    if db is None:
        with SessionLocal() as own:
            return current(own)
    db.rollback()
    stamp = _stamp(db)
    view = _view
    if stamp is None or (view is not None and view.stamp == stamp):
        db.rollback()
        return None if stamp is None else view
    with _lock:
        view = _view
        if view is not None and view.stamp == stamp:
            db.rollback()
            return view
        started = time.monotonic()
        data = compact(db, stamp[0])
        db.rollback()
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        index = search.prepare(data)
        digest = hashlib.sha256(body).hexdigest()[:20]
        view = View(stamp=stamp, body=body, packed=gzip.compress(body, compresslevel=6, mtime=0),
                    etag=f'"c{stamp[0]}-{digest}"', index=index, built_seconds=round(time.monotonic() - started, 3),
                    tracks=len(data["tracks"]))
        _view = view
        logger.info("Catalogue prepared tracks=%s bytes=%s packed=%s seconds=%s", view.tracks, len(body),
                    len(view.packed), view.built_seconds)
        return view


def warm() -> None:
    """Built ahead, after a run: the first browser to ask does not wait for it. Then what the run and the old view
    left behind goes back to the system (``give_back``)."""
    try:
        current()
    except Exception:
        logger.exception("Catalogue could not be prepared")
    give_back()


def give_back() -> bool:
    """Hands memory that is free again back to the system: after a whole run of a large library the C library keeps
    some 50 MB of it otherwise (measured with 50,000 tracks). glibc only; elsewhere nothing happens."""
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        return False
    gc.collect()
    trim(0)
    return True


def search_mode(tracks: int) -> str:
    """Where the interface searches: in the browser, or past ``NEXMIX_SERVER_SEARCH_FROM`` tracks on the server."""
    return "server" if tracks > get_settings().server_search_from else "browser"


# --- The search index ------------------------------------------------------------------------------------------------


def _texts(connection: Connection, generation: int) -> list[tuple[int, str]]:
    album = {row[0]: (row[1], row[2]) for row in connection.execute(
        text("SELECT id, title, artist_id FROM albums WHERE generation = :g"), {"g": generation})}
    artist = dict(connection.execute(text("SELECT id, title FROM artists WHERE generation = :g"),
                                     {"g": generation}).all())
    found = []
    for track_id, title, album_id, own in connection.execute(
            text("SELECT id, title, album_id, artist FROM tracks WHERE generation = :g"), {"g": generation}):
        album_title, artist_id = album.get(album_id, ("", 0))
        parts = (title, own, artist.get(artist_id, ""), album_title)
        found.append((int(track_id), search.fold(" ".join(part for part in parts if part))))
    return found


def reindex_connection(connection: Connection) -> int:
    """The search index made anew from the live generation, on a connection inside its transaction."""
    generation = connection.execute(text("SELECT generation FROM plex_connection LIMIT 1")).scalar() or 0
    connection.execute(text("DELETE FROM track_search"))
    if not generation:
        return 0
    rows = _texts(connection, int(generation))
    for start in range(0, len(rows), 5000):
        connection.execute(text("INSERT INTO track_search (rowid, text) VALUES (:id, :text)"),
                           [{"id": track_id, "text": folded} for track_id, folded in rows[start:start + 5000]])
    return len(rows)


def reindex(db: Session) -> int:
    started = time.monotonic()
    count = reindex_connection(db.connection())
    db.commit()
    logger.info("Search index made tracks=%s seconds=%s", count, round(time.monotonic() - started, 2))
    return count


def candidates(db: Session, view: View, query: str) -> list[int] | None:
    """The tracks (places in the index) whose text holds every word of three letters or more; None when the query has
    none (the search looks at every track then)."""
    long_words = [word for word in search.words(query) if len(word) >= 3]
    if not long_words:
        return None
    # The words are folded: letters and digits only, nothing that means anything to the full-text syntax.
    match = " AND ".join(f'"{word}"' for word in long_words)
    found = db.execute(text("SELECT rowid FROM track_search WHERE track_search MATCH :m"), {"m": match}).scalars()
    position = view.index.position
    return [position[track_id] for track_id in found if track_id in position]


def search_tracks(db: Session, view: View, query: str, filters: search.Filters, limit: int) -> dict[str, Any]:
    """The server's search: the index names the tracks that hold the long words, the rules do the rest."""
    return search.search_tracks(view.index, query, filters, limit, candidates(db, view, query))
