"""The catalogue: where it stands (for everybody signed in), a full run on demand (the operator), and the covers of
the albums, fetched through the server so that the Plex token never reaches a browser."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..deps import DbSession, OperatorAccount, Reader
from ..errors import error
from ..models import CatalogAlbum
from ..services import catalog, catalog_view, covers, plex, plex_link, search

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])

#: A week in the browser; a changed cover is another file at Plex and so another ETag.
COVER_CACHE = "private, max-age=604800"


@router.get("/status", summary="How far the catalogue is: counts, the run under way, the last run, the sonic analysis")
def catalog_status(_reader: Reader, db: DbSession) -> dict[str, Any]:
    found = catalog.status(db)
    # Where the interface searches: in the browser with the whole catalogue, or for a large one on the server.
    found["search"] = catalog_view.search_mode(int(found["counts"]["tracks"]))
    return found


def _view(db: DbSession) -> catalog_view.View:
    view = catalog_view.current(db)
    if view is None:
        raise error("no_catalog", "The catalogue is not there yet.", 409)
    return view


@router.get("", summary="The whole catalogue, compact and packed, for searching in the browser (ETag, 304)",
            response_model=None)
def catalog_compact(request: Request, _reader: Reader, db: DbSession) -> Response:
    view = _view(db)
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
) -> dict[str, Any]:
    started = time.perf_counter()
    view = _view(db)
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
def catalog_facets(_reader: Reader, db: DbSession) -> dict[str, Any]:
    ix = _view(db).index
    return {
        "moods": [{"name": name, "tracks": len(ix.by_mood.get(number, []))} for number, name in enumerate(ix.moods)],
        "groups": [{"key": key, "moods": names} for key, names in search.MOOD_GROUPS],
        "genres": ix.genres,
        "decades": sorted(decade for decade in ix.by_decade if decade),
        "tracks": ix.size,
    }


@router.get("/albums/{album_id}", summary="An album with its tracks in order")
def catalog_album(album_id: Annotated[int, Path(ge=1, le=10**12)], _reader: Reader, db: DbSession) -> dict[str, Any]:
    found = search.album_detail(_view(db).index, album_id)
    if found is None:
        raise error("not_found", "Not found.", 404)
    return found


@router.get("/artists/{artist_id}", summary="An artist: the best-known songs and the albums")
def catalog_artist(artist_id: Annotated[int, Path(ge=1, le=10**12)], _reader: Reader, db: DbSession) -> dict[str, Any]:
    found = search.artist_detail(_view(db).index, artist_id)
    if found is None:
        raise error("not_found", "Not found.", 404)
    return found


@router.post("/sync", status_code=202,
             summary="Fetch the whole catalogue again (a run already waiting or running is not doubled)")
def catalog_sync(_operator: OperatorAccount, db: DbSession) -> JSONResponse:
    row = plex_link.current(db)
    if row is None or not row.section_key:
        raise error("no_plex", "Connect Plex first.", 409)
    started = catalog.request_full(db, "manual")
    return JSONResponse(status_code=202, content={"started": started})


@dataclass
class _Wanted:
    """What a cover request found in the database: the file's name, and what asking Plex takes."""

    name: str
    kept: tuple[bytes, str] | None
    thumb: str = ""
    url: str = ""
    token: str = ""
    client: str = ""


def _look_up(db: DbSession, album_id: int, size: int, tag: str | None) -> _Wanted:
    """The album's cover path, the file kept on disk, and (only when Plex must be asked) the server and its token.
    Runs in a thread of the pool, briefly: the database and the disk, never Plex."""
    # The generation and the album in one state of the database (a full run may switch over in between).
    generation = catalog.pin(db)
    row = plex_link.current(db)
    album = db.get(CatalogAlbum, (generation, album_id)) if row is not None and generation else None
    # Only a path the catalogue took from Plex, and only one shaped like a cover (``plex.THUMB``).
    if row is None or album is None or not plex.THUMB.fullmatch(album.thumb or ""):
        db.rollback()
        raise error("not_found", "Not found.", 404)
    name = covers.name_of(row.machine_id or row.url, album.thumb, size)
    if tag is not None and tag == f'"{name}"':
        db.rollback()
        return _Wanted(name, None)
    kept = covers.read(name)
    if kept is not None:
        db.rollback()
        return _Wanted(name, kept)
    try:
        wanted = _Wanted(name, None, album.thumb, row.url, plex_link.token_of(row), plex_link.client_id(db))
    except plex_link.Unreadable as exc:
        raise error("token_unreadable", "Connect Plex again.", 409) from exc
    finally:
        # What follows may ask Plex for a while, and holds no state of the database meanwhile.
        db.rollback()
    return wanted


def _unavailable(code: str, retry: int) -> HTTPException:
    exc = error(code, "Plex does not deliver covers right now.", 503)
    exc.headers = {"Retry-After": str(max(1, retry))}
    return exc


async def serve_cover(request: Request, album_id: int, db: DbSession, size: int) -> Response:
    """A cover from disk, or from Plex in one of the few places for covers (``covers.fetch``): the route itself holds
    no thread of the server's pool while Plex works, and a Plex that is slow or down never holds up the rest."""
    if size not in plex.COVER_SIZES:
        raise error("invalid_input", "The input is not valid.", 422, fields=["s"])
    tag = request.headers.get("if-none-match", "").strip() or None
    wanted = await run_in_threadpool(_look_up, db, album_id, size, tag)
    headers = {"ETag": f'"{wanted.name}"', "Cache-Control": COVER_CACHE}
    if tag is not None and tag == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    if wanted.kept is not None:
        return Response(wanted.kept[0], media_type=wanted.kept[1], headers=headers)

    def ask_plex() -> tuple[bytes, str]:
        with plex.PlexServer(wanted.url, wanted.token, wanted.client, timeout=20.0) as server:
            data, kind = server.cover(wanted.thumb, size)
        covers.write(wanted.name, data)
        return data, kind

    try:
        data, kind = await covers.fetch(ask_plex)
    except covers.Unavailable as exc:
        raise _unavailable(exc.code, exc.retry) from exc
    except plex.PlexError as exc:
        if exc.code in ("plex_not_found", "no_cover"):
            raise error("not_found", "Not found.", 404) from exc
        if exc.code in covers.OUTAGE:
            covers.plex_failed(exc.code)
            raise _unavailable(exc.code, covers.quiet_for()) from exc
        raise error(exc.code, "Plex did not deliver the cover.", 502) from exc
    covers.plex_delivered()
    return Response(data, media_type=kind, headers=headers)


@router.get("/cover/{album_id}", summary="The cover of an album, 80, 200 or 400 pixels")
async def cover(
    request: Request,
    album_id: Annotated[int, Path(ge=1, le=10**12)],
    _reader: Reader,
    db: DbSession,
    s: Annotated[int, Query(description="80, 200 or 400")] = 200,
) -> Response:
    return await serve_cover(request, album_id, db, s)
