"""Connecting Jellyfin, for the operator: the address, then Quick Connect (a code to confirm in Jellyfin) or an API key
by hand; the music library and the Jellyfin user the playlists will belong to; letting go of it.

No answer ever carries a token, the server's address or one of Jellyfin's ids: Jellyfin serves sound and pictures to
whoever knows an id. A library and a user are named by a handle of nexmix's own (``handle``), which says nothing outside
this nexmix and this server."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, Field

from ..deps import DbSession, OperatorAccount, session_of
from ..errors import error
from ..models import SERVER_JELLYFIN, MediaServer
from ..services import catalog, jellyfin_link, media, mirrors, sync
from ..services.jellyfin import JellyfinError, Library, User

logger = logging.getLogger("nexmix.jellyfin")

router = APIRouter(prefix="/api/v1/jellyfin", tags=["jellyfin"])

#: The status a failure answers with, by its code.
STATUS = {"quick_expired": 404, "quick_off": 409, "bad_url": 422, "bad_token": 422, "address_refused": 422,
          "not_jellyfin": 422, "jellyfin_starting": 503, "jellyfin_timeout": 504, "jellyfin_not_found": 502}
QUICK = re.compile(r"\A[0-9a-f]{24}\Z")
HANDLE = r"^[0-9a-f]{16}$"
ServerId = Annotated[int, Path(ge=1, le=10**9)]


def failed(exc: JellyfinError) -> Exception:
    return error(exc.code, "Jellyfin did not play along.", STATUS.get(exc.code, 502))


def handle(row_or_server: MediaServer | str, key: str) -> str:
    """nexmix's own name for a library or a user of a server: the same each time, and no id of Jellyfin."""
    server = row_or_server if isinstance(row_or_server, str) else row_or_server.machine_id
    return hashlib.sha256(f"nexmix:{server}:{key}".encode()).hexdigest()[:16]


def _libraries(server: MediaServer | str, found: list[Library]) -> list[dict[str, str]]:
    return [{"handle": handle(server, one.key), "title": one.title} for one in found]


def _users(server: MediaServer | str, found: list[User]) -> list[dict[str, Any]]:
    return [{"handle": handle(server, one.id), "name": one.name, "admin": one.admin} for one in found]


def view(row: MediaServer) -> dict[str, Any]:
    return {
        **media.view(row),
        "via": row.via,
        "library": {"handle": handle(row, row.section_key), "title": row.section_title} if row.section_key else None,
        "user": {"handle": handle(row, row.user_id), "name": row.user_name} if row.user_id else None,
        "connected_at": row.connected_at.isoformat() if row.connected_at else None,
    }


def _owner(request: Request, operator: OperatorAccount) -> str:
    return f"{operator.id}:{session_of(request).uid}"


def _row(db: DbSession, server_id: int) -> MediaServer:
    row = media.get(db, server_id)
    if row is None or row.kind != SERVER_JELLYFIN or not row.active:
        raise error("not_found", "Not found.", 404)
    return row


def _start_catalogue(db: DbSession, row: MediaServer) -> None:
    """The catalogue is fetched once the library and the user are chosen (the user's play counts come with it)."""
    if row.section_key and row.user_id:
        catalog.request_full(db, "first", row.id)


@router.get("", summary="The connected Jellyfin servers")
def jellyfin_servers(_operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return {"servers": [view(row) for row in media.active(db) if row.kind == SERVER_JELLYFIN]}


class Address(BaseModel):
    url: str = Field(min_length=1, max_length=300)


@router.post("/quick", summary="Start Quick Connect at a Jellyfin server: a code to confirm in Jellyfin")
def quick_begin(body: Address, request: Request, operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    try:
        ident, pending = jellyfin_link.begin(db, body.url, _owner(request, operator))
    except JellyfinError as exc:
        raise failed(exc) from exc
    return {"id": ident, "code": pending.code, "name": pending.name, "seconds": jellyfin_link.left(pending)}


def _quick_id(quick_id: str) -> str:
    if not QUICK.fullmatch(quick_id):
        raise error("quick_expired", "This code has run out. Get a new one.", 404)
    return quick_id


@router.get("/quick/{quick_id}", summary="Has the code been confirmed in Jellyfin yet")
def quick_state(quick_id: str, request: Request, operator: OperatorAccount) -> dict[str, Any]:
    try:
        pending, done = jellyfin_link.state(_quick_id(quick_id), _owner(request, operator))
    except JellyfinError as exc:
        raise failed(exc) from exc
    return {"done": done, "left": jellyfin_link.left(pending)}


@router.delete("/quick/{quick_id}", status_code=204, summary="Give up a Quick Connect: its secret is forgotten at once")
def quick_cancel(quick_id: str, request: Request, operator: OperatorAccount) -> Response:
    if QUICK.fullmatch(quick_id):
        jellyfin_link.cancel(quick_id, _owner(request, operator))
    return Response(status_code=204)


class Connect(BaseModel):
    #: Quick Connect: the one that was confirmed.
    quick: str | None = Field(default=None, max_length=40)
    #: By hand: the address and an API key of Jellyfin.
    url: str | None = Field(default=None, max_length=300)
    key: str | None = Field(default=None, max_length=200)


@router.post("/connect", summary="Connect the Jellyfin server: after Quick Connect, or by address and API key")
def connect(body: Connect, request: Request, operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    try:
        if body.quick:
            found = jellyfin_link.redeem(db, _quick_id(body.quick), _owner(request, operator))
        else:
            found = jellyfin_link.by_key(db, body.url or "", body.key or "")
    except JellyfinError as exc:
        raise failed(exc) from exc
    if not found.libraries:
        raise error("no_music_library", "This server has no music library.", 409)
    row = jellyfin_link.store(db, found)
    db.commit()
    _start_catalogue(db, row)
    return {"server": view(row), "libraries": _libraries(row, found.libraries), "users": _users(row, found.users)}


@router.get("/{server_id}", summary="One connected Jellyfin server")
def jellyfin_server(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return view(_row(db, server_id))


@router.get("/{server_id}/choices", summary="The music libraries and the users that may be chosen")
def choices(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    row = _row(db, server_id)
    try:
        libraries, users = jellyfin_link.libraries(db, row)
    except jellyfin_link.Unreadable as exc:
        raise error("token_unreadable", "Connect Jellyfin again.", 409) from exc
    except JellyfinError as exc:
        raise failed(exc) from exc
    return {"libraries": _libraries(row, libraries), "users": _users(row, users)}


class Choice(BaseModel):
    handle: str = Field(pattern=HANDLE)


@router.put("/{server_id}/library", summary="Pick the music library; its catalogue is fetched")
def choose_library(server_id: ServerId, body: Choice, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    row = _row(db, server_id)
    try:
        libraries, _users_found = jellyfin_link.libraries(db, row)
    except jellyfin_link.Unreadable as exc:
        raise error("token_unreadable", "Connect Jellyfin again.", 409) from exc
    except JellyfinError as exc:
        raise failed(exc) from exc
    match = next((one for one in libraries if handle(row, one.key) == body.handle), None)
    if match is None:
        raise error("not_found", "Not found.", 404)
    if row.section_key != match.key:
        jellyfin_link.choose_library(db, row, match)
        db.commit()
        logger.info("Jellyfin library chosen server=%s", row.id)
    _start_catalogue(db, row)
    return view(row)


@router.put("/{server_id}/user", summary="Pick the Jellyfin user the playlists will belong to")
def choose_user(server_id: ServerId, body: Choice, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    row = _row(db, server_id)
    try:
        _libraries_found, users = jellyfin_link.libraries(db, row)
    except jellyfin_link.Unreadable as exc:
        raise error("token_unreadable", "Connect Jellyfin again.", 409) from exc
    except JellyfinError as exc:
        raise failed(exc) from exc
    match = next((one for one in users if handle(row, one.id) == body.handle), None)
    if match is None:
        raise error("not_found", "Not found.", 404)
    first = not row.user_id
    jellyfin_link.choose_user(db, row, match)
    db.commit()
    if first:
        _start_catalogue(db, row)
    return view(row)


@router.get("/{server_id}/playlists",
            summary="How the playlists stand in this Jellyfin: saved, waiting, failed, the last look")
def jellyfin_playlists(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return sync.overview(db, _row(db, server_id).id)


@router.post("/{server_id}/playlists/sync", summary="Sync every playlist with this Jellyfin now, and look for new ones")
def sync_playlists(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    row = _row(db, server_id)
    sync.request_all(db, row.id)
    return sync.overview(db, row.id)


@router.delete("/{server_id}", status_code=204,
               summary="Disconnect a Jellyfin server: its token and its catalogue go, its mirrors are forgotten; "
                       "Jellyfin itself stays as it is. Refused (409 server_is_source) while a playlist takes its "
                       "tracks from it")
def disconnect(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> Response:
    row = _row(db, server_id)
    mirrors.may_let_go(db, row)
    mirrors.disconnect(db, row)
    db.commit()
    return Response(status_code=204)
