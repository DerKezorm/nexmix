"""The invitation of a playlist for guests, for the people who may change it (owner, ``edit``, operator):
``/api/v1/playlists/{id}/invite``. Read, make one that waits, change and start it, end it, and take a guest out.

The rights are those of changing the playlist (``services/playlists.load``): a playlist the account may not see is a
404, one it may only see a 403. A key for programs reaches none of it (``deps.Account``)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, Field

from ..deps import Account, DbSession
from ..services import guests, playlists

router = APIRouter(prefix="/api/v1/playlists", tags=["guests"])

PlaylistId = Annotated[int, Path(ge=1, le=2**62)]
GuestId = Annotated[int, Path(ge=1, le=2**62)]


class InviteIn(BaseModel):
    may_add: bool | None = None
    may_sort: bool | None = None
    max: int | None = Field(default=None, ge=1, le=guests.MAX_TRACKS)
    hours: int | None = Field(default=None, ge=1, le=guests.MAX_HOURS)
    preview: bool | None = None
    #: Start the invitation (one that runs already keeps its start).
    start: bool = False


@router.get("/{playlist_id}/invite", summary="The invitation for guests: code, link, QR, rights, the guests")
def read_invite(playlist_id: PlaylistId, request: Request, account: Account, db: DbSession) -> dict[str, Any]:
    playlist, _level = playlists.load(db, account, playlist_id, "edit")
    return guests.host_view(db, request, playlist)


@router.post("/{playlist_id}/invite", summary="Make an invitation that waits to be started (none if one is there)")
def prepare_invite(playlist_id: PlaylistId, request: Request, account: Account, db: DbSession) -> dict[str, Any]:
    return guests.prepare(db, request, account, playlist_id)


@router.put("/{playlist_id}/invite", summary="Change what guests may do; start the invitation with start=true")
def change_invite(playlist_id: PlaylistId, payload: InviteIn, request: Request, account: Account,
                  db: DbSession) -> dict[str, Any]:
    values = payload.model_dump(exclude_none=True, exclude={"start"})
    return guests.change(db, request, account, playlist_id, values, payload.start)


@router.delete("/{playlist_id}/invite", status_code=204,
               summary="End the invitation: every guest is out at once, the link leads nowhere")
def end_invite(playlist_id: PlaylistId, account: Account, db: DbSession) -> Response:
    guests.end(db, account, playlist_id)
    return Response(status_code=204)


@router.delete("/{playlist_id}/invite/guests/{guest_id}", status_code=204,
               summary="Take a guest out at once (what it added stays)")
def remove_guest(playlist_id: PlaylistId, guest_id: GuestId, account: Account, db: DbSession) -> Response:
    guests.remove_guest(db, account, playlist_id, guest_id)
    return Response(status_code=204)
