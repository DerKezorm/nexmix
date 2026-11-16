"""Connecting Music Assistant, for the operator: the address and a long-lived token made in Music Assistant, then the
providers of its library whose tracks count; the playlists there; letting go of it.

No answer ever carries the token, the server's address or one of Music Assistant's ids of its providers: a provider is
named by a handle of nexmix's own (``ma_link.handle``). When the token runs out says the card (``token``), and from
``ma_link.WARN_DAYS`` before that it warns."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, Field

from ..deps import DbSession, OperatorAccount
from ..errors import error
from ..models import SERVER_MA, MediaServer
from ..services import catalog, ma_link, media, mirrors, sync
from ..services.music_assistant import MaError

logger = logging.getLogger("nexmix.musicassistant")

router = APIRouter(prefix="/api/v1/music-assistant", tags=["music-assistant"])

#: The status a failure answers with, by its code.
STATUS = {"bad_url": 422, "bad_token": 422, "address_refused": 422, "not_ma": 422, "ma_too_old": 422,
          "ma_unreachable": 502, "ma_timeout": 504, "ma_starting": 503, "ma_refused": 409}
HANDLE = r"^[0-9a-f]{16}$"
ServerId = Annotated[int, Path(ge=1, le=10**9)]


def failed(exc: MaError) -> Exception:
    return error(exc.code, "Music Assistant did not play along.", STATUS.get(exc.code, 502))


def _providers(row: MediaServer, found: list[ma_link.Provider]) -> list[dict[str, Any]]:
    chosen = set(row.providers or [])
    return [{"handle": ma_link.handle(row, one.instance), "name": one.name, "kind": one.domain,
             "chosen": one.instance in chosen, "available": one.available} for one in found]


def view(db: DbSession, row: MediaServer, offered: list[ma_link.Provider] | None = None) -> dict[str, Any]:
    return {
        **media.view(row),
        "token": ma_link.token_view(row),
        "connected_at": row.connected_at.isoformat() if row.connected_at else None,
        "providers": len(row.providers or []),
        # Chosen providers that are a Plex or Jellyfin server nexmix also knows directly: their tracks stand twice.
        "doubled": ma_link.doubled(db, row, offered) if offered is not None else [],
    }


def _row(db: DbSession, server_id: int) -> MediaServer:
    row = media.get(db, server_id)
    if row is None or row.kind != SERVER_MA or not row.active:
        raise error("not_found", "Not found.", 404)
    return row


def _offered(db: DbSession, row: MediaServer) -> list[ma_link.Provider]:
    try:
        return ma_link.choices(db, row)
    except ma_link.Unreadable as exc:
        raise error("token_unreadable", "Connect Music Assistant again.", 409) from exc
    except MaError as exc:
        raise failed(exc) from exc


@router.get("", summary="The connected Music Assistant servers, with when their tokens run out")
def ma_servers(_operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return {"servers": [view(db, row) for row in media.active(db) if row.kind == SERVER_MA]}


class Connect(BaseModel):
    #: The address of Music Assistant (``http://host:8095``) and a long-lived token made there (profile › tokens).
    url: str = Field(min_length=1, max_length=300)
    token: str = Field(min_length=1, max_length=4096)


@router.post("/connect", summary="Connect a Music Assistant server by its address and a long-lived token")
def connect(body: Connect, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    try:
        found = ma_link.sign_in(body.url, body.token)
    except MaError as exc:
        raise failed(exc) from exc
    if not found.providers:
        raise error("no_music_provider", "This Music Assistant has no music provider.", 409)
    row = ma_link.store(db, found)
    db.commit()
    if row.section_key:
        catalog.request_full(db, "first", row.id)
    return {"server": view(db, row, found.providers), "providers": _providers(row, found.providers)}


@router.get("/{server_id}", summary="One connected Music Assistant server")
def ma_server(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return view(db, _row(db, server_id))


@router.get("/{server_id}/providers", summary="The music providers of its library that may be chosen, and which are")
def providers(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    row = _row(db, server_id)
    offered = _offered(db, row)
    return {"providers": _providers(row, offered), "doubled": ma_link.doubled(db, row, offered)}


class Choice(BaseModel):
    handles: list[Annotated[str, Field(pattern=HANDLE)]] = Field(min_length=1, max_length=50)


@router.put("/{server_id}/providers", summary="Choose the providers whose tracks count; the catalogue is fetched anew")
def choose_providers(server_id: ServerId, body: Choice, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    row = _row(db, server_id)
    offered = _offered(db, row)
    by_handle = {ma_link.handle(row, one.instance): one for one in offered}
    if any(handle not in by_handle for handle in body.handles):
        raise error("not_found", "Not found.", 404)
    if ma_link.choose(db, row, [by_handle[handle].instance for handle in body.handles], offered):
        db.commit()
        logger.info("Music Assistant providers chosen server=%s count=%s", row.id, len(body.handles))
        catalog.request_full(db, "first", row.id)
    return {"server": view(db, row, offered), "providers": _providers(row, offered)}


@router.get("/{server_id}/playlists",
            summary="How the playlists stand in this Music Assistant: saved, waiting, failed, the last look")
def ma_playlists(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return sync.overview(db, _row(db, server_id).id)


@router.post("/{server_id}/playlists/sync",
             summary="Sync every playlist with this Music Assistant now, and look for new ones")
def sync_playlists(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    row = _row(db, server_id)
    sync.request_all(db, row.id)
    return sync.overview(db, row.id)


@router.delete("/{server_id}", status_code=204,
               summary="Disconnect a Music Assistant server: its token and its catalogue go, its mirrors are "
                       "forgotten; Music Assistant itself stays as it is. Refused (409 server_is_source) while a "
                       "playlist takes its tracks from it")
def disconnect(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> Response:
    row = _row(db, server_id)
    mirrors.may_let_go(db, row)
    mirrors.disconnect(db, row)
    db.commit()
    return Response(status_code=204)
