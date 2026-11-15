"""The media servers, as everybody signed in may see them: which there are, what they are called, whether they are
connected and ready, and what they can do. Never where they are or how nexmix signs in there. Connecting a server goes
through ``/api/v1/plex`` and ``/api/v1/jellyfin`` (the operator); letting go of any one of them through here."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, Field

from ..deps import DbSession, OperatorAccount, Reader
from ..errors import error
from ..services import media, mirrors, sources
from ..services.media import NUMBER_LIMIT

router = APIRouter(prefix="/api/v1/servers", tags=["servers"])


@router.get("", summary="The media servers: name, kind, connected or resting, ready, and what each can do")
def servers(_reader: Reader, db: DbSession) -> dict[str, Any]:
    first = media.primary(db)
    return {
        "servers": [{**media.view(row), "capabilities": sources.capabilities(row.kind).view(),
                     "primary": first is not None and row.id == first.id} for row in media.every(db)],
    }


ServerId = Annotated[int, Path(ge=1, le=10**9)]
Track = Annotated[int, Field(ge=1, le=NUMBER_LIMIT)]


@router.delete("/{server_id}", status_code=204,
               summary="Disconnect a media server: its token and its catalogue go, its mirrors are forgotten (the "
                       "playlists there stay as they are). Refused (409 server_is_source) while a playlist takes its "
                       "tracks from it: move them first (``POST /{id}/transfer`` with ``disconnect``)")
def disconnect(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> Response:
    row = media.get(db, server_id)
    if row is None or not row.active:
        raise error("not_found", "Not found.", 404)
    mirrors.may_let_go(db, row)
    mirrors.disconnect(db, row)
    db.commit()
    return Response(status_code=204)


class TransferPreviewIn(BaseModel):
    #: ``mirror``: every playlist of the other servers gets a mirror here; ``move``: this server becomes their source.
    mode: str = Field(pattern="^(mirror|move)$")
    #: Only the playlists of this server (none: of every other one).
    source: Annotated[int, Field(ge=1, le=10**9)] | None = None
    #: Choices by hand, by the track of an entry: the track of this server, or null to leave it out.
    choices: dict[Track, Track | None] = Field(default_factory=dict, max_length=50_000)


class TransferIn(TransferPreviewIn):
    #: Move: the copies on the servers before stay mirrors, or are deleted there.
    old: str = Field(default="keep", pattern="^(keep|delete)$")
    #: Move: let go of this server once none of its playlists is left on it.
    disconnect: Annotated[int, Field(ge=1, le=10**9)] | None = None


@router.post("/{server_id}/transfer/preview",
             summary="Take all over: what copying or moving every playlist of the other servers here would find. "
                     "Nothing is written")
def transfer_preview(server_id: ServerId, payload: TransferPreviewIn, _operator: OperatorAccount,
                     db: DbSession) -> dict[str, Any]:
    target = mirrors._target(db, server_id)
    chosen, smart = mirrors.candidates(db, target.id, payload.mode, payload.source)
    found = mirrors.preview(db, chosen, target.id, payload.choices)
    return {**found, "playlists": len(chosen), "smart": smart}


@router.post("/{server_id}/transfer", status_code=202,
             summary="Take all over: every playlist of the other servers mirrored or moved here, one after another in "
                     "the background")
def transfer(server_id: ServerId, payload: TransferIn, operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    row = mirrors.start_transfer(db, operator, server_id, payload.mode, old=payload.old, choices=payload.choices,
                                 source_id=payload.source, disconnect=payload.disconnect)
    return {"transfer": mirrors.transfer_view(row)}


@router.get("/{server_id}/transfer", summary="How the last \"take all over\" to this server stands (null: none yet)")
def transfer_state(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    if media.get(db, server_id) is None:
        raise error("not_found", "Not found.", 404)
    return {"transfer": mirrors.transfer_view(mirrors.latest_transfer(db, server_id))}


@router.delete("/{server_id}/transfer", summary="Stop the running \"take all over\" after the playlist under way")
def transfer_stop(server_id: ServerId, _operator: OperatorAccount, db: DbSession) -> dict[str, Any]:
    return {"transfer": mirrors.transfer_view(mirrors.stop_transfer(db, server_id))}
