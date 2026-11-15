"""A playlist on more servers than its source (``services/mirrors.py``): its mirrors, copying it to another server
(a mirror kept in step, or once), moving it there, the preview before, an entry's track on a mirror chosen by hand,
and the tracks only in a mirror. Copying and moving belong to the owner and the operator; choosing tracks and taking
over or dropping what is only in a mirror to whoever may change the playlist. Nothing here names a server's own ids or
its address: tracks are nexmix's numbers."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, Field

from ..deps import Account, DbSession, Reader
from ..errors import error
from ..services import mirrors, playlists
from ..services.media import NUMBER_LIMIT

router = APIRouter(prefix="/api/v1/playlists", tags=["mirrors"])

PlaylistId = Annotated[int, Path(ge=1, le=2**62)]
ServerId = Annotated[int, Path(ge=1, le=10**9)]
EntryId = Annotated[int, Path(ge=1, le=2**62)]
Track = Annotated[int, Field(ge=1, le=NUMBER_LIMIT)]
#: The most choices one request carries: one for every entry of the largest playlist, and some.
MAX_CHOICES = 50_000


class ChoicesIn(BaseModel):
    #: The target server (``GET /api/v1/servers``).
    server: Annotated[int, Field(ge=1, le=10**9)]
    #: Choices by hand, by the track of an entry: the track of the target that stands for it, or null to leave it out.
    choices: dict[Track, Track | None] = Field(default_factory=dict, max_length=MAX_CHOICES)


class CopyIn(ChoicesIn):
    #: Copy once: a new playlist with the target as its source, no tie to this one. Else a mirror kept in step.
    once: bool = False


class MoveIn(ChoicesIn):
    #: The copy on the server before: kept as a mirror, or deleted there.
    old: str = Field(default="keep", pattern="^(keep|delete)$")


class ChoiceIn(BaseModel):
    #: The track of the mirror's server that stands for the entry; null: the entry is left out of the mirror.
    track: Track | None = None


class TakeIn(BaseModel):
    #: The track of the playlist's source server the track only in the mirror stands for.
    track: Track


def _manage(db: DbSession, account: Account, playlist_id: int) -> None:
    playlists.load(db, account, playlist_id, "manage")


def _view(db: DbSession, playlist_id: int) -> dict[str, Any]:
    return {"mirrors": mirrors.view(db, playlist_id)}


@router.get("/{playlist_id}/mirrors",
            summary="The playlist's mirrors: on which server, how the sync stands there, the entries not there, and "
                    "the tracks only there")
def read_mirrors(playlist_id: PlaylistId, reader: Reader, db: DbSession) -> dict[str, Any]:
    playlists.load(db, reader, playlist_id)
    return _view(db, playlist_id)


@router.post("/{playlist_id}/mirrors/preview",
             summary="What copying or moving the playlist to a server would find: the counts, and every entry not "
                     "found or found by an unsure step. Nothing is written")
def preview(playlist_id: PlaylistId, payload: ChoicesIn, account: Account, db: DbSession) -> dict[str, Any]:
    playlist, _level = playlists.load(db, account, playlist_id, "manage")
    target = mirrors._target(db, payload.server)
    if playlist.source_server_id == target.id:
        raise error("same_server", "The playlist takes its tracks from this server already.", 409)
    return mirrors.preview(db, [playlist_id], target.id, payload.choices)


@router.post("/{playlist_id}/mirrors", status_code=201,
             summary="Copy the playlist to a server: a mirror kept in step both ways, or (once) a new playlist there")
def copy(playlist_id: PlaylistId, payload: CopyIn, account: Account, db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "manage-live")
    _manage(db, account, playlist_id)
    if payload.once:
        made = mirrors.copy_once(db, playlist_id, payload.server, payload.choices)
        return {"playlist": made.id, "mirrors": mirrors.view(db, playlist_id)}
    mirrors.make_mirror(db, playlist_id, payload.server, payload.choices)
    return {"playlist": playlist_id, **_view(db, playlist_id)}


@router.post("/{playlist_id}/move",
             summary="Move the playlist to a server: it becomes the source, the copy before stays a mirror or goes")
def move(playlist_id: PlaylistId, payload: MoveIn, account: Account, db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "manage-live")
    _manage(db, account, playlist_id)
    found = mirrors.move(db, playlist_id, payload.server, payload.choices, payload.old)
    return {"playlist": playlist_id, "server": payload.server, **found, **_view(db, playlist_id)}


@router.delete("/{playlist_id}/mirrors/{server_id}", status_code=204,
               summary="Let go of a mirror: the playlist on that server stays as it is, nexmix no longer keeps it in "
                       "step")
def forget_mirror(playlist_id: PlaylistId, server_id: ServerId, account: Account, db: DbSession) -> Response:
    _manage(db, account, playlist_id)
    mirrors.forget(db, playlist_id, server_id)
    return Response(status_code=204)


@router.post("/{playlist_id}/mirrors/{server_id}/remake",
             summary="Make a mirror that was deleted on its server there anew")
def remake_mirror(playlist_id: PlaylistId, server_id: ServerId, account: Account, db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "manage-live")
    _manage(db, account, playlist_id)
    mirrors.remake(db, playlist_id, server_id)
    return _view(db, playlist_id)


@router.put("/{playlist_id}/mirrors/{server_id}/entries/{entry_id}",
            summary="Choose the track of the mirror's server that stands for an entry (null: leave it out there)")
def choose(playlist_id: PlaylistId, server_id: ServerId, entry_id: EntryId, payload: ChoiceIn, account: Account,
           db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "edit")
    mirrors.choose(db, playlist_id, server_id, entry_id, payload.track)
    return _view(db, playlist_id)


@router.post("/{playlist_id}/mirrors/{server_id}/extras/{extra_id}/take",
             summary="Take a track that is only in a mirror over as a track of the source: a new entry at the end")
def take_extra(playlist_id: PlaylistId, server_id: ServerId, extra_id: EntryId, payload: TakeIn, account: Account,
               db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "edit")
    return mirrors.take_extra(db, account, playlist_id, server_id, extra_id, payload.track)


@router.delete("/{playlist_id}/mirrors/{server_id}/extras/{extra_id}", status_code=204,
               summary="Drop a track that is only in a mirror: it is taken out of the mirror there")
def drop_extra(playlist_id: PlaylistId, server_id: ServerId, extra_id: EntryId, account: Account,
               db: DbSession) -> Response:
    playlists.load(db, account, playlist_id, "edit")
    mirrors.drop_extra(db, playlist_id, server_id, extra_id)
    return Response(status_code=204)
