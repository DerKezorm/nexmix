"""What a guest reaches, under ``/api/v1/guest``, with the guest cookie of one invitation and nothing else: the playlist
of the invitation, its changes as they happen, adding, taking out and (with the right) sorting, the catalogue for the
search, suggestions and the covers.

Every route asks for the guest of this request (``GuestCaller``): no cookie or an unknown one is 401 ``guest_required``,
a guest taken out is 403 ``guest_removed``, an invitation that ended or ran out (or a playlist in the trash) is 403
``invite_ended``. The guest cookie opens no route of an account, and an account's session opens none of these."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..db import SessionLocal
from ..deps import DbSession
from ..errors import error
from ..services import guests, search, suggestions
from ..services.events import hub, playlist_topic
from . import catalog as catalog_routes
from .join import give_cookie

router = APIRouter(prefix="/api/v1/guest", tags=["guests"])

#: As for the playlists: a comment this often, and a look whether the guest is still in.
PING_SECONDS = 20.0
STREAM_SECONDS = 15 * 60.0


def guest_caller(request: Request, db: DbSession) -> guests.Caller:
    found = guests.caller(db, request)
    guests.READ.take(f"g:{found.guest.id}")
    return found


GuestCaller = Annotated[guests.Caller, Depends(guest_caller)]


class TracksIn(BaseModel):
    tracks: list[Annotated[int, Field(ge=1, le=10**12)]] = Field(min_length=1, max_length=guests.MAX_ADD)


class EntriesIn(BaseModel):
    entries: list[Annotated[int, Field(ge=1, le=2**62)]] = Field(min_length=1, max_length=100)


class RestoreIn(EntriesIn):
    after: Annotated[int, Field(ge=1, le=2**62)] | None = None


class MoveIn(BaseModel):
    entry: Annotated[int, Field(ge=1, le=2**62)]
    after: Annotated[int, Field(ge=1, le=2**62)] | None = None


def _act(who: guests.Caller) -> None:
    guests.ACT.take(f"g:{who.guest.id}")


@router.get("", summary="The playlist as this guest sees it, what it may do, and how much it added")
def read(request: Request, response: Response, who: GuestCaller, db: DbSession) -> dict[str, Any]:
    # The cookie anew, to last as long as the invitation does now: more hours for the invitation are more for its
    # guests too.
    if who.invite.expires_at is not None:
        give_cookie(response, request, request.cookies[guests.GUEST_COOKIE], who.invite.expires_at)
    return guests.view(db, who)


@router.post("/items", summary="Add tracks at the end, within the guest's limit")
def add(payload: TracksIn, who: GuestCaller, db: DbSession) -> dict[str, Any]:
    _act(who)
    result = guests.add(db, who.guest.id, payload.tracks)
    return {**result, "state": _fresh(db, who)}


@router.post("/items/remove", summary="Take entries out: its own, or with the right to sort any")
def remove(payload: EntriesIn, who: GuestCaller, db: DbSession) -> dict[str, Any]:
    _act(who)
    result = guests.remove(db, who.guest.id, payload.entries)
    return {**result, "state": _fresh(db, who)}


@router.post("/items/restore", summary="Bring entries taken out back (the undo), after a given entry")
def restore(payload: RestoreIn, who: GuestCaller, db: DbSession) -> dict[str, Any]:
    _act(who)
    result = guests.bring_back(db, who.guest.id, payload.entries, payload.after)
    return {**result, "state": _fresh(db, who)}


@router.post("/items/move", summary="Move an entry after another, with the right to sort")
def move(payload: MoveIn, who: GuestCaller, db: DbSession) -> dict[str, Any]:
    _act(who)
    result = guests.move(db, who.guest.id, payload.entry, payload.after)
    return {**result, "state": _fresh(db, who)}


@router.post("/leave", status_code=204, summary="Leave the playlist; the cookie is worth nothing after")
def leave(request: Request, who: GuestCaller, db: DbSession) -> Response:
    from .auth import secure_cookie

    guests.leave(db, who.guest.id)
    response = Response(status_code=204)
    response.delete_cookie(guests.GUEST_COOKIE, path=guests.COOKIE_PATH, httponly=True, samesite="lax",
                           secure=secure_cookie(request))
    return response


@router.get("/catalog", summary="The whole catalogue, compact and packed, for the guest's search (ETag, 304)",
            response_model=None)
def catalog(request: Request, _who: GuestCaller, db: DbSession) -> Response:
    return catalog_routes.catalog_compact(request, None, db)  # type: ignore[arg-type]


@router.get("/catalog/search", summary="Search tracks on the server, for a catalogue too large for the browser")
def catalog_search(
    _who: GuestCaller,
    db: DbSession,
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=80)] = 40,
) -> dict[str, Any]:
    return catalog_routes.catalog_search(None, db, q=q, kind="tracks", mood="", decade=0, genre="",  # type: ignore[arg-type]
                                         limit=min(limit, search.TRACK_LIMIT))


@router.get("/suggestions", summary="Songs that fit the last tracks of the playlist, worked out on the server")
def suggest_songs(who: GuestCaller, db: DbSession) -> dict[str, Any]:
    started = time.perf_counter()
    found = suggestions.for_guest(db, who.playlist.id)
    return {**found, "took_ms": round((time.perf_counter() - started) * 1000, 1)}


@router.get("/cover/{album_id}", summary="The cover of an album of the catalogue, 80, 200 or 400 pixels",
            response_model=None)
async def cover(
    request: Request,
    album_id: Annotated[int, Path(ge=1, le=10**12)],
    who: GuestCaller,
    db: DbSession,
    s: Annotated[int, Query(description="80, 200 or 400")] = 200,
) -> Response:
    guests.COVER.take(f"g:{who.guest.id}")
    return await catalog_routes.serve_cover(request, album_id, db, s)


def _fresh(db: DbSession, who: guests.Caller) -> dict[str, Any]:
    """The guest's view after a change, read anew."""
    db.rollback()
    guest = db.get(type(who.guest), who.guest.id, populate_existing=True)
    if guest is None:
        raise error("guest_required", "Open the link of the invitation first.", 401)
    return guests.view(db, guests._check(db, guest))


def _for_guests(message: str) -> str | None:
    """An event of the playlist as a guest hears it: that something changed and the new version, never who did it or
    what the people behind the entries are called beyond the entries (the guest loads its view anew). None: not for
    guests."""
    head, _, rest = message.partition("\n")
    kind = head.removeprefix("event: ").strip()
    try:
        data = json.loads(rest.removeprefix("data: ").strip() or "{}")
    except ValueError:
        data = {}
    if kind == "items":
        return f"event: items\ndata: {json.dumps({'version': data.get('version')})}\n\n"
    if kind == "playlist":
        if data.get("op") in ("deleted", "purged"):
            return "event: gone\ndata: {}\n\n"
        payload = {"version": data.get("version")}
        if data.get("op") == "rename" and isinstance(data.get("name"), str):
            payload["name"] = data["name"]
        return f"event: playlist\ndata: {json.dumps(payload)}\n\n"
    if kind == "invite":
        return "event: invite\ndata: {}\n\n"
    if kind in ("ended", "removed"):
        return "event: gone\ndata: {}\n\n"
    return None


@router.get("/events", summary="Server-Sent Events: changes to the playlist, and the end of the invitation")
async def events(request: Request) -> StreamingResponse:
    # The database in a session of its own, closed at once: a dependency would stay open as long as the stream.
    with SessionLocal() as db:
        who = guests.caller(db, request)
        guests.READ.take(f"g:{who.guest.id}")
        guest_id, invite_id, playlist_id = who.guest.id, who.invite.id, who.playlist.id
        version = who.playlist.version
        expires = who.invite.expires_at
    if not guests.open_stream(guest_id):
        raise error("too_many_streams", "This guest has enough pages open.", 429)
    subscriber = hub.subscribe({playlist_topic(playlist_id), guests.invite_topic(invite_id),
                                guests.guest_topic(guest_id)})
    loop = asyncio.get_running_loop()
    ends = loop.time() + STREAM_SECONDS
    if expires is not None:
        from .. import clock

        ends = min(ends, loop.time() + max(0.0, (expires - clock.now()).total_seconds()) + 1)

    async def stream() -> Any:
        try:
            yield f"event: hello\ndata: {{\"version\": {version}}}\n\n"
            while loop.time() < ends:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(subscriber.queue.get(),
                                                     timeout=min(PING_SECONDS, max(0.05, ends - loop.time())))
                except TimeoutError:
                    if not await asyncio.to_thread(guests.still_in, guest_id):
                        yield "event: gone\ndata: {}\n\n"
                        break
                    yield ": ping\n\n"
                    continue
                said = _for_guests(message)
                if said is None:
                    continue
                if said.startswith("event: gone") or not await asyncio.to_thread(guests.still_in, guest_id):
                    yield "event: gone\ndata: {}\n\n"
                    break
                yield said
            else:
                if not await asyncio.to_thread(guests.still_in, guest_id):
                    yield "event: gone\ndata: {}\n\n"
        finally:
            hub.unsubscribe(subscriber)
            guests.close_stream(guest_id)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
