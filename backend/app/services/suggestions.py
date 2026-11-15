"""What the suggestions need from the server: the songs a person asked not to see again, the songs that often stand in
the same playlists ("often together"), the tracks that sound alike by Plex's sonic analysis, and the suggestions
themselves for whoever does not work them out in the browser (``suggest.py``).

**Not again** is kept per account by the song's key (``songs.py``), so every version of the song stays out and the
choice outlives a new catalogue. Outward a song is named by the id of the version shown for it, as everywhere.

**Often together** counts, for every song, the playlists it shares with the seed: only playlists the asking account
may see (its own, the ones shared with it; the operator's: all). A playlist somebody may not see adds nothing, not
even to a number.

**Sounds alike** asks the tracks' server (Plex: ``/library/metadata/<id>/nearest``, through its connector) only
while the library's sonic analysis is on or running; the answers are kept a few minutes per track.

Every media server has its catalogue (``catalog_view``): the suggestions of a playlist come from its server's.

**A guest** of an invitation gets the same rules from its one playlist only (``for_guest``): no "often together" (it
would count the household's playlists), no "sounds alike" and no hidden songs.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .. import clock
from ..errors import error
from ..models import Account, CatalogTrack, HiddenSuggestion
from . import brakes, catalog, catalog_view, media, playlists, plex_link, sources, suggest
from .plex import PlexError
from .search import Index

logger = logging.getLogger("nexmix.suggestions")

#: Songs of "often together" in one answer, and seeds asked about at most.
TOGETHER_LIMIT = 60
SEEDS = 20
#: "Sounds alike": tracks asked per seed, the farthest that still counts, seeds asked at most, how long kept.
SOUND_LIMIT = 30
SOUND_DISTANCE = 0.6
SOUND_SEEDS = 5
SOUND_KEEP = 600.0
SOUND_CACHE = 512
#: A guest's suggestions: seeded from the last tracks of its playlist, as many as its page shows.
GUEST_SEEDS = 5
GUEST_LIMIT = 15


def view_of(db: Session, server_id: int | None = None) -> catalog_view.View:
    view = catalog_view.current(db, server_id)
    if view is None:
        raise error("no_catalog", "The catalogue is not there yet.", 409)
    return view


def song_of(ix: Index, track: int) -> int | None:
    position = ix.position.get(int(track))
    return ix.song[position] if position is not None else None


# --- Not again -------------------------------------------------------------------------------------------------------


_SHOWN = text(
    "SELECT song_key, id, shown FROM tracks WHERE server_id = :s AND generation = :g AND song_key IN :keys "
    "ORDER BY shown DESC, id"
).bindparams(bindparam("keys", expanding=True))


def hidden_songs(db: Session, account_id: int) -> list[int]:
    """The songs the account asked not to see again, by the version shown for each, as far as the catalogues have
    them (a song by its key: one per server that has it)."""
    keys = list(db.scalars(select(HiddenSuggestion.song_key).where(HiddenSuggestion.account_id == account_id)))
    live = catalog.live(db)
    if not keys or not live:
        return []
    found: dict[tuple[int, str], int] = {}
    for server, generation in live.items():
        for start in range(0, len(keys), 500):
            for key, track, _shown in db.execute(_SHOWN, {"s": server, "g": generation,
                                                          "keys": keys[start:start + 500]}):
                found.setdefault((server, str(key)), int(track))
    return sorted(set(found.values()))


def _track_of(db: Session, track_id: int) -> CatalogTrack | None:
    """The track in the live catalogue of the server it belongs to (a number belongs to one server)."""
    for server, generation in catalog.live(db).items():
        found = db.get(CatalogTrack, (server, generation, track_id))
        if found is not None:
            return found
    return None


def _key_of(db: Session, track_id: int) -> str:
    track = _track_of(db, track_id)
    if track is None or not track.song_key:
        raise error("not_found", "Not found.", 404)
    return track.song_key


def hide(db: Session, account: Account, track_id: int) -> dict[str, Any]:
    """The song of the track is not suggested to the account again (twice is once)."""
    key = _key_of(db, track_id)
    db.rollback()
    db.execute(sqlite_insert(HiddenSuggestion).values(account_id=account.id, song_key=key, created_at=clock.now())
               .on_conflict_do_nothing(index_elements=[HiddenSuggestion.account_id, HiddenSuggestion.song_key],
                                       index_where=HiddenSuggestion.account_id.is_not(None)))
    db.commit()
    return {"songs": hidden_songs(db, account.id)}


def unhide(db: Session, account: Account, track_id: int) -> dict[str, Any]:
    """The undo: the song may be suggested again."""
    key = _key_of(db, track_id)
    db.rollback()
    db.execute(delete(HiddenSuggestion).where(HiddenSuggestion.account_id == account.id,
                                              HiddenSuggestion.song_key == key))
    db.commit()
    return {"songs": hidden_songs(db, account.id)}


# --- Often together --------------------------------------------------------------------------------------------------


_ITEMS = text(
    "SELECT playlist_id, track_id FROM playlist_items WHERE removed_at IS NULL AND playlist_id IN :ids"
).bindparams(bindparam("ids", expanding=True))


def together(db: Session, account: Account, ix: Index, seeds: Iterable[int],
             limit: int = TOGETHER_LIMIT) -> dict[str, Any]:
    """For each song: in how many playlists the account may see it stands with a song of the seed (songs by the
    version shown). The seed's own songs are not counted."""
    wanted = {int(song) for song in list(seeds)[:SEEDS]}
    seen = [playlist.id for playlist, _level in playlists.visible(db, account)]
    songs_of: dict[int, set[int]] = {}
    for start in range(0, len(seen), 500):
        for playlist_id, track in db.execute(_ITEMS, {"ids": seen[start:start + 500]}):
            song = song_of(ix, track)
            if song is not None:
                songs_of.setdefault(int(playlist_id), set()).add(song)
    counts: dict[int, int] = {}
    shared = 0
    for songs in songs_of.values():
        if not songs & wanted:
            continue
        shared += 1
        for song in songs - wanted:
            counts[song] = counts.get(song, 0) + 1
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    return {"songs": [{"song": song, "count": count} for song, count in ranked], "playlists": shared}


# --- Sounds alike ----------------------------------------------------------------------------------------------------


_sound_lock = threading.Lock()
_sound: dict[tuple[str, int], tuple[float, list[tuple[int, float]]]] = {}


def forget() -> None:
    """For the tests."""
    with _sound_lock:
        _sound.clear()


def _kept(server: str, track: int) -> list[tuple[int, float]] | None:
    moment = clock.monotonic()
    with _sound_lock:
        found = _sound.get((server, track))
        if found is None or moment - found[0] > SOUND_KEEP:
            return None
        return found[1]


def _keep(server: str, track: int, found: list[tuple[int, float]]) -> None:
    with _sound_lock:
        if len(_sound) >= SOUND_CACHE:
            for key in sorted(_sound, key=lambda one: _sound[one][0])[: SOUND_CACHE // 4]:
                del _sound[key]
        _sound[(server, track)] = (clock.monotonic(), found)


def sound(db: Session, ix: Index, tracks: Sequence[int], who: int | None = None,
          server_id: int | None = None) -> dict[str, Any]:
    """The songs that sound closest to the tracks (their smallest distance to any of them), closest first; empty while
    the analysis is off or the tracks are not analysed yet, or the server cannot tell. ``state`` is the analysis as the
    catalogue knows it. Each time the server is asked counts against ``who`` (``brakes``: a minute holds 60). The
    tracks are of ``server_id`` (the one of ``ix``; None: the first server connected)."""
    seeds = [int(track) for track in tracks if int(track) in ix.position][:SOUND_SEEDS]
    if not seeds:
        raise error("not_found", "Not found.", 404)
    row = media.get(db, server_id) if server_id is not None else media.primary(db)
    state = catalog.analysis_of(row)["state"]
    if row is None or state == "off" or not sources.capabilities(row.kind).sound:
        return {"state": state, "songs": []}
    server = row.machine_id or row.url
    keys = media.keys(db, row.id, media.TRACK, seeds)
    asked: dict[int, list[tuple[int, float]]] = {}
    missing = []
    for track in seeds:
        kept = _kept(server, track)
        if kept is None:
            missing.append(track)
        else:
            asked[track] = kept
    if missing:
        if who is not None:
            brakes.take("sonic", who)
        db.expunge(row)
        db.rollback()
        try:
            with sources.open_server(db, row, timeout=10.0) as source:
                for track in missing:
                    if track not in keys:
                        continue
                    near = source.similar(keys[track], SOUND_LIMIT, SOUND_DISTANCE)
                    numbers = media.known_numbers(db, row.id, media.TRACK, (key for key, _distance in near))
                    db.rollback()
                    found = [(numbers[key], distance) for key, distance in near if key in numbers]
                    _keep(server, track, found)
                    asked[track] = found
        except plex_link.Unreadable as exc:
            raise error("token_unreadable", "Connect Plex again.", 409) from exc
        except PlexError as exc:
            if exc.code != "plex_not_found":
                raise error(exc.code, "Plex did not answer about the sound.", 502) from exc
    own = {song_of(ix, track) for track in seeds}
    closest: dict[int, float] = {}
    for found in asked.values():
        for other, distance in found:
            song = song_of(ix, other)
            if song is None or song in own:
                continue
            if distance < closest.get(song, float("inf")):
                closest[song] = distance
    ranked = sorted(closest.items(), key=lambda pair: (pair[1], pair[0]))
    return {"state": state, "songs": [{"song": song, "distance": round(distance, 4)} for song, distance in ranked]}


# --- For a guest -----------------------------------------------------------------------------------------------------


def for_guest(db: Session, playlist_id: int) -> dict[str, Any]:
    """What fits the last tracks of a guest's playlist, in the kind "fitting": the rules of ``suggest.py``, without the
    playlist's songs, and nothing of any other playlist (the browser asks the same, ``guestSuggest.ts``)."""
    ix = view_of(db, media.source_of(db, playlist_id) or 0).index
    tracks = [item.track_id for item in playlists.live_items(db, playlist_id)]
    exclude = {song for song in (song_of(ix, track) for track in tracks) if song is not None}
    positions = suggest.seed_positions(ix, tracks)[-GUEST_SEEDS:]
    found = suggest.suggest(ix, positions, mode="fit", exclude=exclude, limit=GUEST_LIMIT)
    return {"tracks": found, "seed": [ix.ids[position] for position in positions]}
