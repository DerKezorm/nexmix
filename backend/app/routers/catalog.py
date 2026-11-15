"""The catalogue: where it stands (for everybody signed in), a full run on demand (the operator), and the covers of
the albums, fetched through the server so that no token, no address and no id of a media server ever reaches a
browser (Jellyfin serves its pictures to whoever knows an id).

Every media server has its catalogue: ``server`` names it (``GET /api/v1/servers``); without it, the first one
connected. A playlist's search uses the catalogue of its server (``server`` of the playlist)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..deps import DbSession, OperatorAccount, Reader
from ..errors import error
from ..models import SERVER_JELLYFIN, SERVER_PLEX, CatalogAlbum
from ..services import catalog, catalog_view, covers, jellyfin, media, plex, plex_link, search, sources
from ..services.media import NUMBER_LIMIT
from ..services.sources.jellyfin import TAG as JELLYFIN_TAG

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])

#: A week in the browser; a changed cover is another file at its server and so another ETag.
COVER_CACHE = "private, max-age=604800"

ServerId = Annotated[int | None, Query(ge=1, le=10**9, description="The media server (none: the first connected)")]


@router.get("/status", summary="How far the catalogue is: counts, the run under way, the last run, the sonic analysis")
def catalog_status(_reader: Reader, db: DbSession, server: ServerId = None) -> dict[str, Any]:
    found = catalog.status(db, server)
    # Where the interface searches: in the browser with the whole catalogue, or for a large one on the server.
    found["search"] = catalog_view.search_mode(int(found["counts"]["tracks"]))
    return found


def _view(db: DbSession, server: int | None = None) -> catalog_view.View:
    view = catalog_view.current(db, server)
    if view is None:
        raise error("no_catalog", "The catalogue is not there yet.", 409)
    return view


@router.get("", summary="The whole catalogue, compact and packed, for searching in the browser (ETag, 304)",
            response_model=None)
def catalog_compact(request: Request, _reader: Reader, db: DbSession, server: ServerId = None) -> Response:
    view = _view(db, server)
    headers = {"ETag": view.etag, "Cache-Control": "private, no-cache", "Vary": "Accept-Encoding"}
    if view.etag in [tag.strip() for tag in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        return Response(view.packed, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
    return Response(view.body, media_type="application/json", headers=headers)


Decade = Annotated[int, Query(ge=0, le=3000, description="A decade of the original year, such as 1990")]


@router.get("/search", summary="Search tracks, albums or artists on the server, by the rules of the browser")
def catalog_search(
    _reader: Reader,
    db: DbSession,
    q: Annotated[str, Query(max_length=200)] = "",
    kind: Annotated[str, Query(pattern="^(tracks|albums|artists)$")] = "tracks",
    mood: Annotated[str, Query(max_length=120, description="A mood, or g:<group>")] = "",
    decade: Decade = 0,
    genre: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = search.TRACK_LIMIT,
    server: ServerId = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    view = _view(db, server)
    if kind == "albums":
        found = search.search_albums(view.index, q)
    elif kind == "artists":
        found = search.search_artists(view.index, q)
    else:
        found = catalog_view.search_tracks(db, view, q, search.Filters(mood=mood, decade=decade, genre=genre), limit)
    found["tracks_in_catalog"] = view.tracks
    found["took_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return found


@router.get("/facets", summary="The moods, genres and decades the filters offer, with how many tracks each has")
def catalog_facets(_reader: Reader, db: DbSession, server: ServerId = None) -> dict[str, Any]:
    ix = _view(db, server).index
    return {
        "moods": [{"name": name, "tracks": len(ix.by_mood.get(number, []))} for number, name in enumerate(ix.moods)],
        "groups": [{"key": key, "moods": names} for key, names in search.MOOD_GROUPS],
        "genres": ix.genres,
        "decades": sorted(decade for decade in ix.by_decade if decade),
        "tracks": ix.size,
    }


@router.get("/albums/{album_id}", summary="An album with its tracks in order")
def catalog_album(album_id: Annotated[int, Path(ge=1, le=NUMBER_LIMIT)], _reader: Reader, db: DbSession,
                  server: ServerId = None) -> dict[str, Any]:
    found = search.album_detail(_view(db, server).index, album_id)
    if found is None:
        raise error("not_found", "Not found.", 404)
    return found


@router.get("/artists/{artist_id}", summary="An artist: the best-known songs and the albums")
def catalog_artist(artist_id: Annotated[int, Path(ge=1, le=NUMBER_LIMIT)], _reader: Reader, db: DbSession,
                   server: ServerId = None) -> dict[str, Any]:
    found = search.artist_detail(_view(db, server).index, artist_id)
    if found is None:
        raise error("not_found", "Not found.", 404)
    return found


@router.post("/sync", status_code=202,
             summary="Fetch the whole catalogue again (a run already waiting or running is not doubled)")
def catalog_sync(_operator: OperatorAccount, db: DbSession, server: ServerId = None) -> JSONResponse:
    row = media.get(db, server) if server is not None else plex_link.current(db)
    if row is None or not row.active or not row.section_key:
        raise error("no_plex", "Connect Plex first.", 409)
    started = catalog.request_full(db, "manual", row.id)
    return JSONResponse(status_code=202, content={"started": started})


@dataclass
class _Wanted:
    """What a cover request found in the database: the file's name, and what asking the server takes."""

    name: str
    kept: tuple[bytes, str] | None
    thumb: str = ""
    url: str = ""
    token: str = ""
    client: str = ""
    machine: str = ""
    kind: str = SERVER_PLEX
    #: Jellyfin: the album's id there (its pictures are asked for by the album).
    key: str = ""
    server: int | None = None


def _shaped(kind: str, thumb: str) -> bool:
    """Only a cover the catalogue took from the server, and only one shaped like one of its kind."""
    if kind == SERVER_JELLYFIN:
        return bool(JELLYFIN_TAG.fullmatch(thumb or ""))
    return bool(plex.THUMB.fullmatch(thumb or ""))


def _look_up(db: DbSession, album_id: int, size: int, tag: str | None, server: int | None) -> _Wanted:
    """The album's cover path, the file kept on disk, and (only when the server must be asked) the server and its
    token. An album number belongs to one server; ``server`` limits the look to that one. Runs in a thread of the pool,
    briefly: the database and the disk, never the media server."""
    # The generations and the album in one state of the database (a full run may switch over in between).
    album = None
    for ident, generation in catalog.live(db).items():
        if server is None or ident == server:
            album = db.get(CatalogAlbum, (ident, generation, album_id)) or album
    row = media.get(db, album.server_id) if album is not None else None
    key = ""
    if row is not None and album is not None and row.kind == SERVER_JELLYFIN:
        key = media.keys(db, row.id, media.ALBUM, [album_id]).get(album_id, "")
        if not jellyfin.ID.fullmatch(key):
            key = ""
    if row is None or album is None or not _shaped(row.kind, album.thumb) or (row.kind == SERVER_JELLYFIN and not key):
        db.rollback()
        raise error("not_found", "Not found.", 404)
    name = covers.name_of(row.machine_id or row.url, f"{key}:{album.thumb}" if key else album.thumb, size)
    if tag is not None and tag == f'"{name}"':
        db.rollback()
        return _Wanted(name, None)
    kept = covers.read(name)
    if kept is not None:
        db.rollback()
        return _Wanted(name, kept)
    try:
        token, client = sources.secret_of(db, row)
        wanted = _Wanted(name, None, album.thumb, row.url, token, client, row.machine_id, row.kind, key, row.id)
    except plex_link.Unreadable as exc:
        raise error("token_unreadable", "Connect the media server again.", 409) from exc
    finally:
        # What follows may ask the server for a while, and holds no state of the database meanwhile.
        db.rollback()
    return wanted


def _unavailable(code: str, retry: int) -> HTTPException:
    exc = error(code, "The media server does not deliver covers right now.", 503)
    exc.headers = {"Retry-After": str(max(1, retry))}
    return exc


async def serve_cover(request: Request, album_id: int, db: DbSession, size: int,
                      server: int | None = None) -> Response:
    """A cover from disk, or from its server in one of the few places for covers (``covers.fetch``): the route itself
    holds no thread of the server's pool while the media server works, and one that is slow or down never holds up the
    rest. ``server``: only an album of that server (a guest sees its playlist's server only)."""
    if size not in plex.COVER_SIZES:
        raise error("invalid_input", "The input is not valid.", 422, fields=["s"])
    tag = request.headers.get("if-none-match", "").strip() or None
    wanted = await run_in_threadpool(_look_up, db, album_id, size, tag, server)
    headers = {"ETag": f'"{wanted.name}"', "Cache-Control": COVER_CACHE}
    if tag is not None and tag == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    if wanted.kept is not None:
        return Response(wanted.kept[0], media_type=wanted.kept[1], headers=headers)
    label = "Jellyfin" if wanted.kind == SERVER_JELLYFIN else "Plex"

    def ask_server() -> tuple[bytes, str]:
        if wanted.kind == SERVER_JELLYFIN:
            with sources.JellyfinSource(wanted.url, wanted.token, wanted.client, "", "", timeout=20.0) as jelly:
                data, kind = jelly.cover(wanted.key, size)
        else:
            with sources.PlexSource(wanted.url, wanted.token, wanted.client, wanted.machine, "",
                                    timeout=20.0) as source:
                data, kind = source.cover(wanted.thumb, size)
        covers.write(wanted.name, data)
        return data, kind

    try:
        data, kind = await covers.fetch(ask_server, wanted.server)
    except covers.Unavailable as exc:
        raise _unavailable(exc.code, exc.retry) from exc
    except plex.PlexError as exc:
        if exc.code in ("plex_not_found", "jellyfin_not_found", "no_cover"):
            raise error("not_found", "Not found.", 404) from exc
        if exc.code in covers.OUTAGE:
            covers.plex_failed(exc.code, wanted.server, label)
            raise _unavailable(exc.code, covers.quiet_for(wanted.server)) from exc
        raise error(exc.code, "The media server did not deliver the cover.", 502) from exc
    covers.plex_delivered(wanted.server, label)
    return Response(data, media_type=kind, headers=headers)


@router.get("/cover/{album_id}", summary="The cover of an album, 80, 200 or 400 pixels")
async def cover(
    request: Request,
    album_id: Annotated[int, Path(ge=1, le=NUMBER_LIMIT)],
    _reader: Reader,
    db: DbSession,
    s: Annotated[int, Query(description="80, 200 or 400")] = 200,
) -> Response:
    return await serve_cover(request, album_id, db, s)
