"""Playlists under ``/api/v1/playlists``: the list (and the trash), making one, reading, renaming, deleting and bringing
it back, its entries (add, take out, bring back, move), its shares, its sync with Plex, and its live events.

The rights are checked at every route (``services/playlists.load``): a playlist the asking account may not see is a 404
like one that is not there. Reading goes with the session, a device token or a read key; changing needs the session or
a device (never a key, ``deps.Account``)."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..db import SessionLocal
from ..deps import Account, DbSession, Reader, still_speaks_for
from ..errors import error
from ..services import guests, playlists, sync
from ..services.events import hub, playlist_topic
from ..services.media import NUMBER_LIMIT
from .auth import profile_of

logger = logging.getLogger("nexmix.playlists")

router = APIRouter(prefix="/api/v1/playlists", tags=["playlists"])

PlaylistId = Annotated[int, Path(ge=1, le=2**62)]
AccountId = Annotated[int, Path(ge=1, le=2**62)]

#: A comment this often keeps proxies from closing a quiet stream; the rights are looked at again each time.
PING_SECONDS = 20.0
#: A stream ends after this long and the browser opens a new one (a fresh look at the rights, a fresh connection).
STREAM_SECONDS = 15 * 60.0
#: Streams open at once per account (its browsers, tabs and devices together): enough for a household's use, a bound
#: for a script that would open thousands.
STREAMS_PER_ACCOUNT = 24
_streams: dict[int, int] = {}
_streams_lock = threading.Lock()


def _open_stream(account_id: int) -> bool:
    with _streams_lock:
        if _streams.get(account_id, 0) >= STREAMS_PER_ACCOUNT:
            return False
        _streams[account_id] = _streams.get(account_id, 0) + 1
        return True


def _close_stream(account_id: int) -> None:
    with _streams_lock:
        left = _streams.get(account_id, 0) - 1
        if left > 0:
            _streams[account_id] = left
        else:
            _streams.pop(account_id, None)


def forget() -> None:
    """For the tests."""
    with _streams_lock:
        _streams.clear()


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=400)


class CreateIn(NameIn):
    #: The media server it takes its tracks from (``GET /api/v1/servers``); none: the first one connected.
    server: Annotated[int, Field(ge=1, le=10**9)] | None = None


class TracksIn(BaseModel):
    tracks: list[Annotated[int, Field(ge=1, le=NUMBER_LIMIT)]] = Field(min_length=1, max_length=1000)


class EntriesIn(BaseModel):
    entries: list[Annotated[int, Field(ge=1, le=2**62)]] = Field(min_length=1, max_length=1000)


class RestoreIn(EntriesIn):
    #: The entry they come back after; None: at the start.
    after: Annotated[int, Field(ge=1, le=2**62)] | None = None


class MoveIn(BaseModel):
    entry: Annotated[int, Field(ge=1, le=2**62)]
    #: The entry it goes after; None: to the start.
    after: Annotated[int, Field(ge=1, le=2**62)] | None = None


class ShareIn(BaseModel):
    access: str = Field(pattern="^(read|edit)$")


@router.get("", summary="The playlists the account sees, or (trash=true) the deleted ones it may bring back")
def list_playlists(reader: Reader, db: DbSession, trash: bool = Query(False)) -> dict[str, Any]:
    if trash:
        playlists.purge(db)
    found = playlists.visible(db, reader, trash=trash)
    views = playlists.with_hidden(db, reader, [playlists.card(db, playlist, level) for playlist, level in found])
    # What the account's overview leaves out: the ones it hid (``hidden``), and the smart playlists of Plex unless it
    # chose to see them. The list itself has all of them.
    return {"playlists": views, "trash_days": playlists.TRASH_DAYS,
            "show_smart_playlists": profile_of(reader.profile)["show_smart_playlists"]}


@router.post("", status_code=201,
             summary="Make a playlist (empty, owned by the account), taking its tracks from a media server")
def create_playlist(payload: CreateIn, account: Account, db: DbSession) -> dict[str, Any]:
    playlist = playlists.create(db, account, payload.name, payload.server)
    return playlists.detail(db, playlist, playlists.OWNER)


@router.get("/{playlist_id}", summary="A playlist with its entries in order")
def read_playlist(playlist_id: PlaylistId, reader: Reader, db: DbSession) -> dict[str, Any]:
    playlist, level = playlists.load(db, reader, playlist_id)
    return playlists.with_hidden(db, reader, [playlists.detail(db, playlist, level)])[0]


@router.put("/{playlist_id}", summary="Rename a playlist")
def rename_playlist(playlist_id: PlaylistId, payload: NameIn, account: Account, db: DbSession) -> dict[str, Any]:
    return playlists.rename(db, account, playlist_id, payload.name)


@router.delete("/{playlist_id}",
               summary="Move a playlist to the trash (30 days), or with purge=true delete it from there")
def delete_playlist(playlist_id: PlaylistId, account: Account, db: DbSession,
                    purge: bool = Query(False)) -> dict[str, Any]:
    if purge:
        playlists.purge_one(db, account, playlist_id)
        return {"deleted": True}
    playlist = playlists.trash(db, account, playlist_id)
    return playlists.card(db, playlist, playlists.access_of(db, account, playlist) or playlists.OWNER)


@router.post("/{playlist_id}/restore", summary="Bring a playlist back from the trash")
def restore_playlist(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    playlist = playlists.restore(db, account, playlist_id)
    level = playlists.access_of(db, account, playlist) or playlists.OWNER
    return playlists.detail(db, playlist, level)


@router.put("/{playlist_id}/hidden",
            summary="Hide the playlist from the own overview (this account only; Plex and the others see no change)")
def hide_playlist(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    return playlists.set_hidden(db, account, playlist_id, True)


@router.delete("/{playlist_id}/hidden", summary="Show a playlist hidden from the own overview again")
def show_playlist(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    return playlists.set_hidden(db, account, playlist_id, False)


@router.post("/{playlist_id}/items", summary="Add tracks at the end (ones already in it are skipped)")
def add_items(playlist_id: PlaylistId, payload: TracksIn, account: Account, db: DbSession) -> dict[str, Any]:
    return playlists.add(db, account, playlist_id, payload.tracks)


@router.post("/{playlist_id}/items/remove", summary="Take entries out (ones already gone are no error)")
def remove_items(playlist_id: PlaylistId, payload: EntriesIn, account: Account, db: DbSession) -> dict[str, Any]:
    return playlists.remove(db, account, playlist_id, payload.entries)


@router.post("/{playlist_id}/items/restore", summary="Bring entries taken out back (the undo), after a given entry")
def restore_items(playlist_id: PlaylistId, payload: RestoreIn, account: Account, db: DbSession) -> dict[str, Any]:
    return playlists.bring_back(db, account, playlist_id, payload.entries, payload.after)


@router.post("/{playlist_id}/items/move", summary="Move an entry after another (an entry gone meanwhile: no effect)")
def move_item(playlist_id: PlaylistId, payload: MoveIn, account: Account, db: DbSession) -> dict[str, Any]:
    return playlists.move(db, account, playlist_id, payload.entry, payload.after)


@router.get("/{playlist_id}/shares", summary="Who the playlist is shared with, and with whom it could be")
def read_shares(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    playlist, _level = playlists.load(db, account, playlist_id, "share")
    return playlists.shares_view(db, playlist)


@router.put("/{playlist_id}/shares/{account_id}", summary="Share with a member: to see (read) or to change (edit)")
def put_share(playlist_id: PlaylistId, account_id: AccountId, payload: ShareIn, account: Account,
              db: DbSession) -> dict[str, Any]:
    found = playlists.share(db, account, playlist_id, account_id, payload.access)
    guests.recheck_inviters(db)
    return found


@router.delete("/{playlist_id}/shares/{account_id}", summary="Share no longer with a member")
def delete_share(playlist_id: PlaylistId, account_id: AccountId, account: Account, db: DbSession) -> dict[str, Any]:
    found = playlists.unshare(db, account, playlist_id, account_id)
    guests.recheck_inviters(db)
    return found


@router.post("/{playlist_id}/sync", summary="Sync the playlist with Plex now (also: try again after a failure)")
def sync_playlist(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    # Whoever may change the playlist (a smart one too: it is read from Plex again).
    _playlist, level = playlists.load(db, account, playlist_id)
    if level not in playlists.CAN_EDIT:
        raise error("read_only", "This playlist is shared with you to listen to, not to change.", 403)
    return sync.request(db, playlist_id)


@router.post("/{playlist_id}/plex-order",
             summary="Take the order Plex had when it was sorted there and here at the same time (the note's undo)")
def take_plex_order(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "edit")
    wanted = sync.plex_order(db, playlist_id)
    if wanted is None:
        raise error("no_note", "There is no order of Plex to take.", 409)
    change = playlists.sort_like(db, account, playlist_id, wanted)
    sync.drop_note(db, playlist_id)
    return change


@router.delete("/{playlist_id}/plex-order", summary="Keep nexmix's order: the note about Plex's goes")
def keep_own_order(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "edit")
    sync.drop_note(db, playlist_id)
    return sync.view(db, playlist_id)


@router.post("/{playlist_id}/missing/remove",
             summary="Take over what the media server lost: the entries kept for it are taken out here too")
def take_loss(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "edit")
    entries = sync.missing_tracks(db, playlist_id)
    db.rollback()
    change = playlists.remove(db, account, playlist_id, entries)
    sync.drop_note(db, playlist_id, "loss")
    return change


@router.delete("/{playlist_id}/missing",
               summary="Keep what the media server lost: the note goes, the entries go back after the catalogue run")
def keep_loss(playlist_id: PlaylistId, account: Account, db: DbSession) -> dict[str, Any]:
    playlists.load(db, account, playlist_id, "edit")
    sync.drop_note(db, playlist_id, "loss")
    return sync.view(db, playlist_id)


def _still_sees(request: Request, account_id: int, playlist_id: int) -> bool:
    """Whether the stream may go on: its credential still speaks for the account (not signed out, revoked or blocked),
    and the account still sees the playlist."""
    from ..models import Account as AccountRow

    if still_speaks_for(request) != account_id:
        return False
    with SessionLocal() as db:
        row = db.get(AccountRow, account_id)
        if row is None or row.blocked_at is not None:
            return False
        try:
            playlists.load(db, row, playlist_id)
        except HTTPException:
            return False
    return True


@router.get("/{playlist_id}/events", summary="Server-Sent Events: changes to the playlist as they happen")
async def playlist_events(playlist_id: PlaylistId, request: Request, reader: Reader) -> StreamingResponse:
    # The database is asked in a session of its own, closed again at once: a dependency would stay open as long as the
    # stream does.
    with SessionLocal() as db:
        playlist, _level = playlists.load(db, reader, playlist_id)
        version = playlist.version
    account_id = reader.id
    if not _open_stream(account_id):
        raise error("too_many_streams", "This account has enough pages open.", 429)
    subscriber = hub.subscribe({playlist_topic(playlist_id)})
    loop = asyncio.get_running_loop()
    ends = loop.time() + STREAM_SECONDS

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
                    if not await asyncio.to_thread(_still_sees, request, account_id, playlist_id):
                        yield "event: gone\ndata: {}\n\n"
                        break
                    yield ": ping\n\n"
                    continue
                # A share taken back or the playlist deleted: whoever may not see it any more hears no more.
                if message.startswith(("event: access", "event: playlist")) and not await asyncio.to_thread(
                        _still_sees, request, account_id, playlist_id):
                    yield "event: gone\ndata: {}\n\n"
                    break
                yield message
        finally:
            hub.unsubscribe(subscriber)
            _close_stream(account_id)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
