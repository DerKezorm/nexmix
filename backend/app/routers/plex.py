"""Connecting Plex, for the operator: signing in through plex.tv with a PIN or typing an address and token, picking
the server and the music library, letting go of it. No answer ever carries a token or the address of a server the
operator did not choose."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..deps import DbSession, OperatorAccount, session_of
from ..errors import error
from ..services import catalog, plex_link, plex_sync
from ..services.plex import PlexError, Section

logger = logging.getLogger("nexmix.plex")

router = APIRouter(prefix="/api/v1/plex", tags=["plex"])

#: The status a failure of Plex answers with, by its code.
STATUS = {"pin_expired": 404, "bad_url": 422, "bad_token": 422, "address_refused": 422, "library_gone": 409}


def failed(exc: PlexError) -> Exception:
    return error(exc.code, "Plex did not play along.", STATUS.get(exc.code, 502))


def _view(db: DbSession) -> dict[str, Any]:
    row = plex_link.current(db)
    if row is None:
        return {"connected": False}
    return {
        "connected": True,
        "via": row.via,
        "server_name": row.server_name,
        "owned": row.owned,
        "library": {"key": row.section_key, "title": row.section_title} if row.section_key else None,
        "connected_at": row.connected_at.isoformat(),
    }


@router.get("", summary="The Plex connection")
def plex_status(_operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return _view(db)


def _owner(request: Request, operator: OperatorAccount) -> str:
    """The session a sign-in through plex.tv belongs to: the account and its session."""
    return f"{operator.id}:{session_of(request).uid}"


@router.post("/pin", summary="Start signing in with Plex: a PIN and the page of Plex to open")
def pin_begin(request: Request, operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    try:
        pin = plex_link.begin(db, _owner(request, operator))
    except PlexError as exc:
        raise failed(exc) from exc
    return {"id": pin["id"], "url": pin["url"], "seconds": plex_link.PIN_SECONDS}


@router.get("/pin/{pin_id}", summary="Has the operator agreed at Plex yet, and which servers with music are there")
def pin_poll(pin_id: str, request: Request, operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    if not pin_id.isdigit() or len(pin_id) > 20:
        raise error("pin_expired", "This sign-in has run out. Start it again.", 404)
    try:
        pending = plex_link.poll(db, pin_id, _owner(request, operator))
    except PlexError as exc:
        raise failed(exc) from exc
    if pending.servers is None:
        return {"done": False, "left": max(0, int(plex_link.PIN_SECONDS - plex_link.started_ago(pending)))}
    return {
        "done": True,
        "offered": pending.offered,
        "servers": [
            {"index": index, "name": server.name, "owned": server.owned,
             "libraries": [{"key": one.key, "title": one.title} for one in server.sections]}
            for index, server in enumerate(pending.servers)
        ],
    }


@router.delete("/pin/{pin_id}", status_code=204,
               summary="Give up a sign-in with Plex: what it found is forgotten at once")
def pin_cancel(pin_id: str, request: Request, operator: OperatorAccount) -> Response:
    # The same answer whether there was one or not: nobody learns about the sign-ins of others.
    if pin_id.isdigit() and len(pin_id) <= 20:
        plex_link.cancel(pin_id, _owner(request, operator))
    return Response(status_code=204)


class Connect(BaseModel):
    #: Signed in through plex.tv: the PIN and the number of the server in its list.
    pin_id: str | None = Field(default=None, max_length=20)
    server: int | None = Field(default=None, ge=0, le=200)
    #: By hand: the address and the token.
    url: str | None = Field(default=None, max_length=300)
    token: str | None = Field(default=None, max_length=300)


@router.post("/connect", summary="Connect the server chosen after the sign-in, or one given by address and token")
def connect(body: Connect, request: Request, operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    try:
        if body.pin_id:
            if body.server is None:
                raise PlexError("pin_expired", 404)
            found = plex_link.chosen(body.pin_id, body.server, _owner(request, operator))
            via = "plextv"
        else:
            found = plex_link.by_hand(db, body.url or "", body.token or "")
            via = "manual"
    except PlexError as exc:
        raise failed(exc) from exc
    if not found.sections:
        raise error("no_music_library", "This server has no music library.", 409)
    row = plex_link.store(db, found, via)
    db.commit()
    if body.pin_id:
        plex_link.finish(body.pin_id)
    if row.section_key:
        catalog.request_full(db, "first")
    return {**_view(db), "libraries": [{"key": one.key, "title": one.title} for one in found.sections]}


def _sections(db: DbSession) -> list[Section]:
    row = plex_link.current(db)
    if row is None:
        raise error("no_plex", "Connect Plex first.", 409)
    try:
        with plex_link.server_of(db, row, timeout=15.0) as server:
            return server.music_sections()
    except plex_link.Unreadable as exc:
        raise error("token_unreadable", "Connect Plex again.", 409) from exc
    except PlexError as exc:
        raise failed(exc) from exc


@router.get("/libraries", summary="The music libraries of the connected server")
def libraries(_operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return {"libraries": [{"key": one.key, "title": one.title} for one in _sections(db)]}


class LibraryChoice(BaseModel):
    key: str = Field(min_length=1, max_length=12, pattern=r"^\d+$")


@router.put("/library", summary="Pick the music library; its catalogue is fetched")
def choose_library(body: LibraryChoice, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    match = next((one for one in _sections(db) if one.key == body.key), None)
    if match is None:
        raise error("not_found", "Not found.", 404)
    row = plex_link.current(db)
    if row is None:
        raise error("no_plex", "Connect Plex first.", 409)
    if row.section_key != match.key:
        plex_link.choose_library(db, row, match)
        db.commit()
        logger.info("Plex library chosen")
    catalog.request_full(db, "first")
    return _view(db)


@router.delete("", status_code=204,
               summary="Disconnect Plex: the token and the catalogue go; Plex itself stays as it is")
def disconnect(_operator: OperatorAccount, db: DbSession) -> Response:
    plex_link.disconnect(db)
    db.commit()
    return Response(status_code=204)


@router.get("/playlists", summary="How the playlists stand in Plex: synced, waiting, failed, smart ones, the last look")
def plex_playlists(_operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return plex_sync.overview(db)


@router.post("/playlists/sync", summary="Sync every playlist with Plex now, and look for new ones there")
def sync_playlists(_operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    plex_sync.request_all(db)
    return plex_sync.overview(db)
