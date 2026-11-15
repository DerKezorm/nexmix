"""Suggestions under ``/api/v1/suggestions``: the songs to add, worked out on the server (``services/suggest.py``, the
same rules as in the browser), filling by mood, the two voices the browser asks for (often together, sounds alike), and
the songs a person asked not to see again.

Songs are named by the id of the version shown for them (``song`` in the compact catalogue). Reading goes with a
session, a device token or a read key; "not again" changes something and needs a session or a device. The catalogue
is the one of the playlist's server (``playlist``), else of ``server``, else of the first one connected."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from ..deps import Account, DbSession, Reader
from ..models import Account as AccountRow
from ..services import catalog, media, playlists, suggest, suggestions
from ..services.media import NUMBER_LIMIT
from ..services.search import Index

router = APIRouter(prefix="/api/v1/suggestions", tags=["suggestions"])

TrackId = Annotated[int, Field(ge=1, le=NUMBER_LIMIT)]
TrackIds = Annotated[list[TrackId], Query(max_length=suggestions.SEEDS)]
PlaylistId = Annotated[int | None, Query(ge=1, le=2**62, description="Leave out its songs; seed from its last five")]
ServerId = Annotated[int | None, Query(ge=1, le=10**9, description="The media server (none: the first connected)")]


def _server(db: DbSession, reader: AccountRow, playlist_id: int | None, server: int | None) -> int | None:
    """The server whose catalogue to suggest from: the playlist's (one the account may see), else the one asked for."""
    if playlist_id is not None:
        playlist, _level = playlists.load(db, reader, playlist_id)
        return playlist.source_server_id or 0
    return server


def _playlist_songs(db: DbSession, reader: AccountRow, ix: Index,
                    playlist_id: int | None) -> tuple[list[int], set[int]]:
    """The tracks of a playlist the account may see (in order) and their songs; nothing without one."""
    if playlist_id is None:
        return [], set()
    playlist, _level = playlists.load(db, reader, playlist_id)
    tracks = [item.track_id for item in playlists.live_items(db, playlist.id)]
    return tracks, {song for song in (suggestions.song_of(ix, track) for track in tracks) if song is not None}


@router.get("", summary="Songs that fit a few tracks (the seed), by mood, style, genre, decade and the two voices")
def suggest_songs(
    reader: Reader,
    db: DbSession,
    seed: TrackIds = [],  # noqa: B006 - FastAPI copies the default
    mode: Annotated[str, Query(pattern="^(fit|known|new)$")] = "fit",
    playlist: PlaylistId = None,
    limit: Annotated[int, Query(ge=1, le=50)] = suggest.LIMIT,
    server: ServerId = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    chosen_server = _server(db, reader, playlist, server)
    view = suggestions.view_of(db, chosen_server)
    ix = view.index
    tracks, exclude = _playlist_songs(db, reader, ix, playlist)
    chosen = list(seed) or tracks[-5:]
    positions = suggest.seed_positions(ix, chosen)
    hidden = suggestions.hidden_songs(db, reader.id)
    seed_songs = [ix.song[position] for position in positions]
    together = {one["song"]: one["count"] for one in suggestions.together(db, reader, ix, seed_songs)["songs"]}
    sound: dict[int, float] = {}
    if positions and catalog.analysis_of(media.get(db, view.server))["state"] in ("running", "on"):
        try:
            sound = {one["song"]: one["distance"]
                     for one in suggestions.sound(db, ix, [ix.ids[p] for p in positions], reader.id,
                                                  view.server)["songs"]}
        except Exception:  # noqa: BLE001 - the voice is a help; without it the rest still suggests
            sound = {}
    found = suggest.suggest(ix, positions, mode=mode, exclude=exclude, hidden=hidden, together=together, sound=sound,
                            limit=limit)
    return {"tracks": found, "seed": [ix.ids[position] for position in positions],
            "took_ms": round((time.perf_counter() - started) * 1000, 1)}


@router.get("/mood", summary="Songs of the chosen moods for about a given length (filling by mood)")
def fill_by_mood(
    reader: Reader,
    db: DbSession,
    moods: Annotated[list[Annotated[str, Field(max_length=120)]], Query(min_length=1, max_length=40)],
    decades: Annotated[list[Annotated[int, Field(ge=1000, le=3000)]], Query(max_length=20)] = [],  # noqa: B006
    minutes: Annotated[int, Query(ge=5, le=600)] = 60,
    hits: Annotated[int, Query(ge=0, le=100, description="0: discoveries, 100: hits")] = 55,
    playlist: PlaylistId = None,
    seed: Annotated[int, Query(ge=0, le=2**32 - 1, description="The same seed, the same mix")] = 1,
    server: ServerId = None,
) -> dict[str, Any]:
    ix = suggestions.view_of(db, _server(db, reader, playlist, server)).index
    _tracks, exclude = _playlist_songs(db, reader, ix, playlist)
    found = suggest.build(ix, moods, decades=decades, minutes=minutes, hits=hits, exclude=exclude,
                          hidden=suggestions.hidden_songs(db, reader.id), seed=seed)
    return {"tracks": found, "duration": sum(track["duration"] for track in found)}


@router.get("/together", summary="Songs that often stand in the same playlists as these songs (the ones you may see)")
def often_together(reader: Reader, db: DbSession,
                   songs: Annotated[list[TrackId], Query(min_length=1, max_length=20)],
                   server: ServerId = None) -> dict[str, Any]:
    ix = suggestions.view_of(db, server).index
    return suggestions.together(db, reader, ix, songs)


@router.get("/sonic", summary="Songs that sound like these tracks, by Plex's sonic analysis (empty while it is off)")
def sounds_alike(reader: Reader, db: DbSession,
                 track: Annotated[list[TrackId], Query(min_length=1, max_length=5)],
                 server: ServerId = None) -> dict[str, Any]:
    view = suggestions.view_of(db, server)
    return suggestions.sound(db, view.index, track, reader.id, view.server)


@router.get("/hidden", summary="The songs the account asked not to be suggested again")
def hidden(reader: Reader, db: DbSession) -> dict[str, Any]:
    return {"songs": suggestions.hidden_songs(db, reader.id)}


class HideIn(BaseModel):
    track: int = Field(ge=1, le=NUMBER_LIMIT)


@router.post("/hidden", summary="Not again: the song of a track is no longer suggested to the account")
def hide(payload: HideIn, account: Account, db: DbSession) -> dict[str, Any]:
    return suggestions.hide(db, account, payload.track)


@router.delete("/hidden/{track_id}", summary="The song of a track may be suggested again (the undo)")
def unhide(track_id: Annotated[int, Path(ge=1, le=NUMBER_LIMIT)], account: Account, db: DbSession) -> dict[str, Any]:
    return suggestions.unhide(db, account, track_id)
