"""Playlists: who may do what, the changes to their entries, and what the interface and programs see of them.

**Rights.** A playlist belongs to the account that made it. A member sees their own and the ones shared with them
(``read``: to see, ``edit``: to change the entries and the name as well); the operator sees and may do everything. Only
the owner and the operator delete, bring back from the trash and share. A playlist somebody may not see does not exist
for them (404, the same as one that is not there); one they see but may not change says so (403).

**Changes** are operations of their own (add, take out, bring back, move, rename), each about entry ids, never about a
whole list: two people changing the same playlist at once both get what they did. Each takes the write lock of the
database first (``_begin``: the version of the playlist goes up before anything is read), so the server does them one
after the other, and the checks and the change are one step. A change that finds nothing to do (a track already there,
an entry already taken out, a move of an entry that is gone) is no error and changes nothing, not even the version.

**Events**: every change goes to the browsers that have the playlist open (``events.py``) with the new version and the
order of all entries; a browser that missed one sees the gap in the versions and loads the playlist anew.

**Trash**: a deleted playlist stays 30 days (``TRASH_DAYS``), then goes for good (``purge``).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, delete, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..errors import error
from ..models import (
    OPERATOR,
    SHARE_EDIT,
    SHARE_READ,
    Account,
    HiddenPlaylist,
    Playlist,
    PlaylistItem,
    PlaylistShare,
)
from . import media, merge, sync
from .events import hub, playlist_topic

logger = logging.getLogger("nexmix.playlists")

TRASH_DAYS = 30
#: How long an entry taken out can be brought back (the undo of the interface needs seconds).
REMOVED_KEEP = timedelta(days=1)
MAX_ITEMS = 5000
#: Playlists one account may hold (not counting the trash): each one is a playlist in Plex too.
MAX_PLAYLISTS = 500
MAX_NAME = 200
SUMMARY_COVERS = 4
SUMMARY_PEOPLE = 5
SUMMARY_MOODS = 6

#: What the asking account may do with a playlist.
OWNER, OPERATOR_ACCESS = "owner", "operator"
CAN_EDIT = frozenset({OWNER, OPERATOR_ACCESS, SHARE_EDIT})
CAN_MANAGE = frozenset({OWNER, OPERATOR_ACCESS})


# --- Rights ----------------------------------------------------------------------------------------------------------


def access_of(db: Session, account: Account, playlist: Playlist) -> str | None:
    """``owner``, ``operator``, ``edit``, ``read``, or None (may not see it). A playlist in the trash is seen only by
    those who may bring it back."""
    if playlist.owner_id is not None and playlist.owner_id == account.id:
        level: str | None = OWNER
    elif account.role == OPERATOR:
        level = OPERATOR_ACCESS
    else:
        share = db.get(PlaylistShare, (playlist.id, account.id))
        level = share.access if share is not None else None
    if playlist.deleted_at is not None and level not in CAN_MANAGE:
        return None
    return level


def _not_found() -> Exception:
    return error("not_found", "Not found.", 404)


def load(db: Session, account: Account, playlist_id: int, need: str = "view") -> tuple[Playlist, str]:
    """The playlist and the account's access, or 404 (not there, or not to be seen) / 403 (seen, but not that right)."""
    playlist = db.execute(select(Playlist).where(Playlist.id == playlist_id)
                          .execution_options(populate_existing=True)).scalar_one_or_none()
    if playlist is None:
        raise _not_found()
    level = access_of(db, account, playlist)
    if level is None:
        raise _not_found()
    if need == "edit" and level not in CAN_EDIT:
        raise error("read_only", "This playlist is shared with you to listen to, not to change.", 403)
    if need in ("manage", "share") and level not in CAN_MANAGE:
        raise error("not_owner", "Only the owner and the operator may do this.", 403)
    if need in ("edit", "manage-live") and playlist.deleted_at is not None:
        raise error("in_trash", "The playlist is in the trash. Bring it back first.", 409)
    if need in ("edit", "manage") and playlist.deleted_at is None and sync.is_smart(db, playlist.id):
        # Plex fills a smart playlist from its rules: it is changed there, never here.
        raise error("smart_playlist", "A smart playlist of Plex is only read here. Change it in Plex.", 409)
    return playlist, level


def _begin(db: Session, account: Account, playlist_id: int, need: str) -> tuple[Playlist, str]:
    """The write lock first, then the checks: the version goes up as the very first statement of the transaction (a
    write takes SQLite's lock, a read would not), so nothing another request changes can come between the checks and
    the change. The caller commits, or rolls back when there was nothing to do."""
    db.rollback()
    now = clock.now()
    taken = db.execute(update(Playlist).where(Playlist.id == playlist_id)
                       .values(version=Playlist.version + 1, updated_at=now)).rowcount
    if taken != 1:
        db.rollback()
        raise _not_found()
    try:
        return load(db, account, playlist_id, need)
    except Exception:
        db.rollback()
        raise


# --- Views -----------------------------------------------------------------------------------------------------------


def person(account: Account | None) -> dict[str, Any] | None:
    """A person as the playlists show it to others (owner, who added, the sharing dialog, the live events): by the
    display name. The name to sign in with goes along only for an account without a display name, where it is all
    there is to tell the person by; the operator sees every name in the accounts."""
    if account is None:
        return None
    return {"id": account.id, "name": account.display_name or account.name, "display_name": account.display_name,
            "avatar": account.avatar_at.isoformat() if account.avatar_at else None}


def _live_generation(db: Session, server_id: int | None) -> int:
    """The live generation of a server, with the state of the database fixed for the queries that follow
    (``catalog.pin``)."""
    from . import catalog

    if server_id is None:
        catalog.hold(db)
        return 0
    return catalog.pin(db, server_id)


def _chunks(values: Sequence[int], size: int = 500) -> Iterable[Sequence[int]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


@dataclass(frozen=True)
class Known:
    """What the live catalogue says about a track."""

    title: str
    artist: str
    album: str
    album_id: int
    cover: bool
    year: int | None
    duration: int
    song: int
    artist_id: int
    #: Plex's ``guid`` of the track (empty: the catalogue has none yet).
    guid: str = ""


_TRACKS_OF = text(
    "SELECT t.id, t.title, t.artist, t.duration, t.song_key, t.guid, t.album_id, a.title AS album, a.year, a.thumb, "
    "a.artist_id, ar.title AS album_artist FROM tracks t "
    "LEFT JOIN albums a ON a.server_id = t.server_id AND a.generation = t.generation AND a.id = t.album_id "
    "LEFT JOIN artists ar ON ar.server_id = t.server_id AND ar.generation = t.generation AND ar.id = a.artist_id "
    "WHERE t.server_id = :s AND t.generation = :g AND t.id IN :ids"
).bindparams(bindparam("ids", expanding=True))

#: The moods of tracks, albums or artists, one statement per table (the names are ours, never from outside).
_MOODS_OF = {
    table: text(
        "SELECT l.item_id, t.name FROM " + table + " l JOIN tags t ON t.server_id = l.server_id "  # noqa: S608
        "AND t.generation = l.generation AND t.id = l.tag_id WHERE l.server_id = :s AND l.generation = :g "
        "AND t.kind = 'mood' AND l.item_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    for table in ("track_tags", "album_tags", "artist_tags")
}


def catalogue_of(db: Session, track_ids: Iterable[int], *, server_id: int | None) -> dict[int, Known]:
    """The tracks of the live catalogue of a server (a playlist's source) among ``track_ids``, with the id of the
    version shown for their song. A track of another server is not among them."""
    generation = _live_generation(db, server_id)
    wanted = sorted({int(track) for track in track_ids})
    if not generation or not wanted:
        return {}
    rows: dict[int, Any] = {}
    for part in _chunks(wanted):
        for row in db.execute(_TRACKS_OF, {"s": server_id, "g": generation, "ids": list(part)}):
            rows[int(row.id)] = row
    keys = sorted({row.song_key for row in rows.values()})
    shown: dict[str, int] = {}
    for part_start in range(0, len(keys), 500):
        part_keys = keys[part_start:part_start + 500]
        for key, track in db.execute(
                select(_track_table().c.song_key, _track_table().c.id)
                .where(_track_table().c.server_id == server_id, _track_table().c.generation == generation,
                       _track_table().c.shown.is_(True),
                       _track_table().c.song_key.in_(part_keys))):
            shown.setdefault(key, int(track))
    return {
        track: Known(
            title=row.title or "", artist=row.artist or row.album_artist or "", album=row.album or "",
            album_id=int(row.album_id or 0), cover=bool(row.thumb), year=row.year or None,
            duration=int(row.duration or 0), song=shown.get(row.song_key, track), artist_id=int(row.artist_id or 0),
            guid=str(row.guid or ""),
        )
        for track, row in rows.items()
    }


def _track_table() -> Any:
    from ..models import CatalogTrack

    return CatalogTrack.__table__


def _seconds(milliseconds: int) -> int:
    return round((milliseconds or 0) / 1000)


def item_view(item: PlaylistItem, known: Known | None, people: dict[int, Account]) -> dict[str, Any]:
    adder = people.get(item.added_by_user) if item.added_by_user is not None else None
    return {
        "id": item.id,
        "track": item.track_id,
        "title": known.title if known else item.title,
        "artist": known.artist if known else item.artist,
        "album": known.album if known else item.album,
        "album_id": (known.album_id if known else item.album_id) or None,
        "cover": bool(known.cover) if known else False,
        "year": known.year if known else item.year,
        "duration": _seconds(known.duration if known else item.duration),
        "song": known.song if known else None,
        "missing": known is None,
        "from_plex": bool(item.from_plex),
        "plex_hint": item.plex_hint or None,
        "added_by": person(adder),
        "added_by_name": (adder.display_name or adder.name) if adder else item.added_by_name,
        "added_at": item.added_at.isoformat(),
    }


def live_items(db: Session, playlist_id: int) -> list[PlaylistItem]:
    return list(db.scalars(select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id,
                                                      PlaylistItem.removed_at.is_(None))
                           .order_by(PlaylistItem.position, PlaylistItem.id)
                           .execution_options(populate_existing=True)))


def people_of(db: Session, ids: Iterable[int | None]) -> dict[int, Account]:
    wanted = sorted({int(one) for one in ids if one is not None})
    found: dict[int, Account] = {}
    for part in _chunks(wanted):
        for account in db.scalars(select(Account).where(Account.id.in_(part))):
            found[account.id] = account
    return found


def moods_of(db: Session, known: dict[int, Known], tracks: Sequence[int], *, server_id: int | None) -> list[str]:
    """The moods the entries have most of: each track's effective moods (its own 1, the album's 0.7, the artist's 0.4),
    summed over the entries; the strongest few, ties by name."""
    generation = _live_generation(db, server_id)
    if not generation or not tracks:
        return []

    def tags(table: str, ids: set[int]) -> dict[int, list[str]]:
        found: dict[int, list[str]] = {}
        ordered = sorted(ids)
        for part in _chunks(ordered):
            for item, name in db.execute(_MOODS_OF[table], {"s": server_id, "g": generation, "ids": list(part)}):
                found.setdefault(int(item), []).append(str(name))
        return found

    present = [track for track in tracks if track in known]
    own = tags("track_tags", set(present))
    albums = tags("album_tags", {known[track].album_id for track in present})
    artists = tags("artist_tags", {known[track].artist_id for track in present})
    total: dict[str, float] = {}
    for track in present:
        weights: dict[str, float] = {}
        for name in artists.get(known[track].artist_id, []):
            weights[name] = 0.4
        for name in albums.get(known[track].album_id, []):
            weights[name] = 0.7
        for name in own.get(track, []):
            weights[name] = 1.0
        for name, weight in weights.items():
            total[name] = total.get(name, 0.0) + weight
    return [name for name, _weight in sorted(total.items(), key=lambda pair: (-pair[1], pair[0]))[:SUMMARY_MOODS]]


def summary(db: Session, items: Sequence[PlaylistItem], known: dict[int, Known] | None = None,
            people: dict[int, Account] | None = None, *, moods: bool = False,
            server_id: int | None = None) -> dict[str, Any]:
    """Count, length, the covers of the first albums, who added what (the first few), and when asked the moods. The
    tracks are the ones of ``server_id``'s catalogue (the playlist's source)."""
    known = catalogue_of(db, (item.track_id for item in items), server_id=server_id) if known is None else known
    people = people_of(db, (item.added_by_user for item in items)) if people is None else people
    covers: list[int] = []
    for item in items:
        found = known.get(item.track_id)
        if found and found.cover and found.album_id and found.album_id not in covers:
            covers.append(found.album_id)
            if len(covers) == SUMMARY_COVERS:
                break
    contributors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        adder = people.get(item.added_by_user) if item.added_by_user is not None else None
        key = f"a{adder.id}" if adder else f"n{item.added_by_name}"
        if key in seen or (adder is None and not item.added_by_name):
            continue
        seen.add(key)
        contributors.append(person(adder) if adder else {"id": None, "name": item.added_by_name,
                                                          "display_name": item.added_by_name, "avatar": None})
    duration = sum(_seconds(known[item.track_id].duration if item.track_id in known else item.duration)
                   for item in items)
    found: dict[str, Any] = {
        "count": len(items), "duration": duration, "covers": covers,
        "people": contributors[:SUMMARY_PEOPLE], "contributors": len(contributors),
    }
    if moods:
        found["moods"] = moods_of(db, known, [item.track_id for item in items], server_id=server_id)
    return found


def _share_count(db: Session, playlist_id: int) -> int:
    return int(db.scalar(select(func.count()).select_from(PlaylistShare)
                         .where(PlaylistShare.playlist_id == playlist_id)) or 0)


def card(db: Session, playlist: Playlist, level: str) -> dict[str, Any]:
    from . import guests

    items = live_items(db, playlist.id)
    owner = db.get(Account, playlist.owner_id) if playlist.owner_id is not None else None
    purge_at = playlist.deleted_at + timedelta(days=TRASH_DAYS) if playlist.deleted_at else None
    plex = sync.view(db, playlist.id)
    found = summary(db, items, server_id=playlist.source_server_id)
    if plex["smart"] and plex["total"] is not None:
        # A smart playlist shows its first entries; the number is all of them.
        found["count"] = plex["total"]
    return {
        "id": playlist.id,
        "name": playlist.name,
        "description": playlist.description,
        "owner": person(owner),
        "access": level,
        "shares": _share_count(db, playlist.id),
        "version": playlist.version,
        "created_at": playlist.created_at.isoformat(),
        "updated_at": playlist.updated_at.isoformat(),
        "deleted_at": playlist.deleted_at.isoformat() if playlist.deleted_at else None,
        "purge_at": purge_at.isoformat() if purge_at else None,
        # Whole days still in the trash, counted up: "1" on its last day.
        "days_left": ceil_days((purge_at - clock.now()).total_seconds()) if purge_at else None,
        # The guests of a running invitation; None without one.
        "guests": guests.live_count(db, playlist.id),
        **found,
        "smart": plex["smart"],
        "plex": plex,
        # The server the playlist takes its tracks from (None: none connected yet).
        "server": playlist.source_server_id,
    }


def detail(db: Session, playlist: Playlist, level: str) -> dict[str, Any]:
    items = live_items(db, playlist.id)
    known = catalogue_of(db, (item.track_id for item in items), server_id=playlist.source_server_id)
    people = people_of(db, (item.added_by_user for item in items))
    view = card(db, playlist, level)
    count = view["count"]
    view.update(summary(db, items, known, people, moods=True, server_id=playlist.source_server_id))
    view["count"] = count
    view["items"] = [item_view(item, known.get(item.track_id), people) for item in items]
    return view


# --- Lists -----------------------------------------------------------------------------------------------------------


def visible(db: Session, account: Account, *, trash: bool = False) -> list[tuple[Playlist, str]]:
    """The playlists the account sees, most recently changed first; ``trash``: the deleted ones it may bring back."""
    query = select(Playlist)
    if trash:
        query = query.where(Playlist.deleted_at.is_not(None))
        if account.role != OPERATOR:
            query = query.where(Playlist.owner_id == account.id)
    else:
        query = query.where(Playlist.deleted_at.is_(None))
        if account.role != OPERATOR:
            shared = select(PlaylistShare.playlist_id).where(PlaylistShare.account_id == account.id)
            query = query.where(or_(Playlist.owner_id == account.id, Playlist.id.in_(shared)))
    found = []
    for playlist in db.scalars(query.order_by(Playlist.updated_at.desc(), Playlist.id.desc())):
        level = access_of(db, account, playlist)
        if level is not None:
            found.append((playlist, level))
    return found


# --- Hidden from the own view -----------------------------------------------------------------------------------------


def hidden_ids(db: Session, account: Account) -> set[int]:
    """The playlists the account hid from its own view."""
    return set(db.scalars(select(HiddenPlaylist.playlist_id).where(HiddenPlaylist.account_id == account.id)))


def with_hidden(db: Session, account: Account, views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each view says whether the asking account hid it (``hidden``)."""
    hidden = hidden_ids(db, account)
    for view in views:
        view["hidden"] = view["id"] in hidden
    return views


def set_hidden(db: Session, account: Account, playlist_id: int, hidden: bool) -> dict[str, Any]:
    """Hides a playlist from the account's own view, or shows it again: nothing else changes (not Plex, not the
    rights, not what others see). Whoever may see the playlist may hide it. Twice is once."""
    load(db, account, playlist_id)
    if hidden:
        db.execute(sqlite_insert(HiddenPlaylist)
                   .values(account_id=account.id, playlist_id=playlist_id, created_at=clock.now())
                   .on_conflict_do_nothing(index_elements=[HiddenPlaylist.account_id, HiddenPlaylist.playlist_id]))
    else:
        db.execute(delete(HiddenPlaylist).where(HiddenPlaylist.account_id == account.id,
                                                HiddenPlaylist.playlist_id == playlist_id))
    db.commit()
    return {"id": playlist_id, "hidden": hidden}


# --- Making, naming, the trash ----------------------------------------------------------------------------------------


def clean_name(name: Any) -> str:
    cleaned = " ".join(str(name or "").split())
    if not cleaned or len(cleaned) > MAX_NAME:
        raise error("invalid_name", "A name of 1 to 200 characters.", 422, fields=["name"])
    return cleaned


def create(db: Session, account: Account, name: str, server: int | None = None) -> Playlist:
    """A new playlist of the account, taking its tracks from ``server`` (a connected media server; None: the first one
    connected). It goes in as the first write of its transaction (SQLite's write lock), and only then are the
    account's playlists counted: two at once never pass ``MAX_PLAYLISTS`` together."""
    now = clock.now()
    cleaned = clean_name(name)
    db.rollback()
    if server is not None:
        chosen = media.get(db, server)
        if chosen is None or not chosen.active:
            raise error("invalid_server", "No such media server is connected.", 422, fields=["server"])
    else:
        chosen = media.primary(db)
    playlist = Playlist(owner_id=account.id, name=cleaned, version=1, created_at=now, updated_at=now,
                        source_server_id=chosen.id if chosen is not None else None)
    db.add(playlist)
    db.flush()
    held = int(db.scalar(select(func.count()).select_from(Playlist)
                         .where(Playlist.owner_id == account.id, Playlist.deleted_at.is_(None))) or 0)
    if held > MAX_PLAYLISTS:
        db.rollback()
        raise error("too_many_playlists", f"An account holds at most {MAX_PLAYLISTS} playlists.", 409,
                    max=MAX_PLAYLISTS)
    sync.mark(db, playlist.id)
    db.commit()
    logger.info("Playlist made id=%s", playlist.id)
    return playlist


def _publish(playlist_id: int, event: str, payload: dict[str, Any]) -> None:
    hub.publish(playlist_topic(playlist_id), event, payload)


def rename(db: Session, account: Account, playlist_id: int, name: str) -> dict[str, Any]:
    cleaned = clean_name(name)
    playlist, _level = _begin(db, account, playlist_id, "edit")
    if playlist.name == cleaned:
        db.rollback()
        playlist, _level = load(db, account, playlist_id)
        return {"version": playlist.version, "name": playlist.name}
    playlist.name = cleaned
    sync.mark(db, playlist_id)
    db.commit()
    payload = {"version": playlist.version, "op": "rename", "name": cleaned, "by": person(account)}
    _publish(playlist_id, "playlist", payload)
    return payload


def trash(db: Session, account: Account, playlist_id: int) -> Playlist:
    playlist, _level = _begin(db, account, playlist_id, "manage")
    if playlist.deleted_at is None:
        playlist.deleted_at = clock.now()
        # Gone from Plex at once.
        sync.mark(db, playlist_id, at_once=True)
        db.commit()
        logger.info("Playlist moved to the trash id=%s", playlist.id)
        _publish(playlist_id, "playlist", {"version": playlist.version, "op": "deleted", "by": person(account)})
        # Its invitation for guests ends with it: brought back, it does not let the old guests in again.
        from . import guests

        guests.end_with_playlist(db, playlist_id)
    else:
        db.rollback()
    return playlist


def restore(db: Session, account: Account, playlist_id: int) -> Playlist:
    playlist, _level = _begin(db, account, playlist_id, "manage")
    if playlist.deleted_at is not None:
        playlist.deleted_at = None
        # Made in Plex anew.
        sync.restored(db, playlist_id)
        db.commit()
        logger.info("Playlist brought back id=%s", playlist.id)
        _publish(playlist_id, "playlist", {"version": playlist.version, "op": "restored", "by": person(account)})
    else:
        db.rollback()
    return playlist


def purge_one(db: Session, account: Account, playlist_id: int) -> None:
    """For good, and only from the trash: a playlist in use is first moved there (one more step, one more question)."""
    playlist, _level = _begin(db, account, playlist_id, "manage")
    if playlist.deleted_at is None:
        db.rollback()
        raise error("not_in_trash", "Move the playlist to the trash first.", 409)
    sync.dropping(db, playlist_id)
    db.execute(delete(Playlist).where(Playlist.id == playlist_id))
    db.commit()
    logger.info("Playlist deleted for good id=%s", playlist_id)
    _publish(playlist_id, "playlist", {"version": playlist.version, "op": "purged"})


def purge(db: Session, now: datetime | None = None) -> int:
    """What lay in the trash longer than ``TRASH_DAYS`` goes for good; entries taken out longer than a day ago too."""
    moment = now or clock.now()
    gone = db.execute(delete(Playlist).where(Playlist.deleted_at.is_not(None),
                                             Playlist.deleted_at <= moment - timedelta(days=TRASH_DAYS))).rowcount
    db.execute(delete(PlaylistItem).where(PlaylistItem.removed_at.is_not(None),
                                          PlaylistItem.removed_at <= moment - REMOVED_KEEP))
    db.commit()
    if gone:
        logger.info("Trash emptied playlists=%s", gone)
    return int(gone or 0)


# --- The entries -----------------------------------------------------------------------------------------------------


def reorder(db: Session, ordered: list[PlaylistItem]) -> None:
    """Positions 0, 1, 2 ... in this order, written only where they changed."""
    changes = [{"id": item.id, "position": number} for number, item in enumerate(ordered) if item.position != number]
    if changes:
        db.execute(update(PlaylistItem), changes)
    for number, item in enumerate(ordered):
        item.position = number


def _changed(db: Session, account: Account | Any, playlist: Playlist, op: str, ordered: list[PlaylistItem],
             new: list[PlaylistItem], **extra: Any) -> dict[str, Any]:
    """Commits the change and tells everybody who has the playlist open; the same answer goes to the one who made it.
    ``account`` is the account, or the guest (``services/guests.py``) that made the change. The change goes to Plex two
    seconds later (more changes in those seconds go along)."""
    sync.mark(db, playlist.id)
    db.commit()
    known = catalogue_of(db, (item.track_id for item in ordered), server_id=playlist.source_server_id)
    own = isinstance(account, Account)
    people = people_of(db, [*(item.added_by_user for item in ordered), account.id if own else None])
    payload = {
        "version": playlist.version,
        "op": op,
        "by": person(account) if own else {"id": None, "name": account.name, "display_name": account.name,
                                           "avatar": None, "guest": True},
        "entries": [item_view(item, known.get(item.track_id), people) for item in new],
        "order": [item.id for item in ordered],
        "summary": summary(db, ordered, known, people, moods=True, server_id=playlist.source_server_id),
        **extra,
    }
    _publish(playlist.id, "items", payload)
    return payload


def _nothing(db: Session, account: Account, playlist_id: int, **extra: Any) -> dict[str, Any]:
    db.rollback()
    playlist, _level = load(db, account, playlist_id)
    return {"version": playlist.version, "op": "none", "entries": [], **extra}


def add(db: Session, account: Account, playlist_id: int, tracks: Sequence[int]) -> dict[str, Any]:
    """The tracks at the end, in the order given; ones already in the playlist and ones the catalogue does not know are
    left out (``skipped``)."""
    wanted: list[int] = []
    for track in tracks:
        if int(track) not in wanted:
            wanted.append(int(track))
    playlist, _level = _begin(db, account, playlist_id, "edit")
    ordered = live_items(db, playlist_id)
    there = {item.track_id for item in ordered}
    # Only tracks of the playlist's own server: its search shows no other.
    known = catalogue_of(db, wanted, server_id=playlist.source_server_id)
    fresh = [track for track in wanted if track in known and track not in there]
    skipped = [track for track in wanted if track not in fresh]
    if not fresh:
        return _nothing(db, account, playlist_id, skipped=skipped)
    if len(ordered) + len(fresh) > MAX_ITEMS:
        db.rollback()
        raise error("playlist_full", "A playlist holds at most 5000 tracks.", 409, max=MAX_ITEMS)
    now = clock.now()
    name = account.display_name or account.name
    new = []
    for track in fresh:
        found = known[track]
        item = PlaylistItem(playlist_id=playlist_id, track_id=track, position=len(ordered) + len(new),
                            added_by_user=account.id, added_by_name=name[:80], added_at=now, title=found.title[:500],
                            artist=found.artist[:500], album=found.album[:500], album_id=found.album_id,
                            year=found.year, duration=found.duration, guid=found.guid[:200])
        db.add(item)
        new.append(item)
    try:
        db.flush()
    except IntegrityError:
        # Only the unique index can say no here, and only if the lock did not hold: nothing is half done.
        db.rollback()
        raise error("busy", "The server is busy. Try again in a moment.", 503) from None
    return _changed(db, account, playlist, "add", ordered + new, new, skipped=skipped)


def remove(db: Session, account: Account, playlist_id: int, entries: Sequence[int]) -> dict[str, Any]:
    """Takes the entries out (they stay a day for the undo); ones already gone are no error."""
    wanted = {int(entry) for entry in entries}
    playlist, _level = _begin(db, account, playlist_id, "edit")
    ordered = live_items(db, playlist_id)
    going = [item for item in ordered if item.id in wanted]
    if not going:
        return _nothing(db, account, playlist_id, removed=[])
    now = clock.now()
    for item in going:
        item.removed_at = now
    kept = [item for item in ordered if item.id not in wanted]
    db.flush()
    reorder(db, kept)
    return _changed(db, account, playlist, "remove", kept, [], removed=[item.id for item in going])


def bring_back(db: Session, account: Account, playlist_id: int, entries: Sequence[int],
               after: int | None) -> dict[str, Any]:
    """Entries taken out come back with who added them, after ``after`` (None: at the start). When ``after`` is gone
    meanwhile, they come back at the number they had (as far as the list reaches now). An entry whose track is in the
    playlist again stays out."""
    wanted = [int(entry) for entry in dict.fromkeys(entries)]
    playlist, _level = _begin(db, account, playlist_id, "edit")
    ordered = live_items(db, playlist_id)
    there = {item.track_id for item in ordered}
    found = {item.id: item for item in db.scalars(
        select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id, PlaylistItem.id.in_(wanted),
                                   PlaylistItem.removed_at.is_not(None)).execution_options(populate_existing=True))}
    back: list[PlaylistItem] = []
    for entry in wanted:
        item = found.get(entry)
        if item is not None and item.track_id not in there:
            back.append(item)
            there.add(item.track_id)
    if not back:
        return _nothing(db, account, playlist_id)
    if len(ordered) + len(back) > MAX_ITEMS:
        db.rollback()
        raise error("playlist_full", "A playlist holds at most 5000 tracks.", 409, max=MAX_ITEMS)
    ids = [item.id for item in ordered]
    if after is None:
        at = 0
    elif after in ids:
        at = ids.index(after) + 1
    else:
        at = min(back[0].position, len(ordered))
    for item in back:
        item.removed_at = None
    db.flush()
    result = ordered[:at] + back + ordered[at:]
    reorder(db, result)
    return _changed(db, account, playlist, "restore", result, back)


def move(db: Session, account: Account, playlist_id: int, entry: int, after: int | None) -> dict[str, Any]:
    """The entry right after ``after`` (None: to the start). An entry or a place that is gone meanwhile: nothing
    happens, and the answer says so (``moved``: false)."""
    playlist, _level = _begin(db, account, playlist_id, "edit")
    ordered = live_items(db, playlist_id)
    ids = [item.id for item in ordered]
    if entry not in ids or (after is not None and after not in ids) or after == entry:
        return _nothing(db, account, playlist_id, moved=False)
    item = ordered[ids.index(entry)]
    rest = [one for one in ordered if one.id != entry]
    at = 0 if after is None else [one.id for one in rest].index(after) + 1
    result = rest[:at] + [item] + rest[at:]
    if [one.id for one in result] == ids:
        return _nothing(db, account, playlist_id, moved=False)
    reorder(db, result)
    return _changed(db, account, playlist, "move", result, [], moved=True, entry=entry, after=after)


def sort_like(db: Session, account: Account, playlist_id: int, wanted: Sequence[int]) -> dict[str, Any]:
    """The entries sorted as ``wanted`` (tracks) says, the ones it does not name after the entry they followed: the
    undo of "sorted in Plex at the same time" takes Plex's order this way."""
    playlist, _level = _begin(db, account, playlist_id, "edit")
    ordered = live_items(db, playlist_id)
    by_track = {item.track_id: item for item in ordered}
    result = [by_track[track] for track in merge.arrange([item.track_id for item in ordered], wanted)]
    if [item.id for item in result] == [item.id for item in ordered]:
        return _nothing(db, account, playlist_id, moved=False)
    reorder(db, result)
    return _changed(db, account, playlist, "move", result, [], moved=True)


# --- Shares ----------------------------------------------------------------------------------------------------------


def shares_view(db: Session, playlist: Playlist) -> dict[str, Any]:
    """Who it is shared with, and the members it could be shared with (the operator sees everything anyway)."""
    rows = db.execute(select(PlaylistShare, Account).join(Account, Account.id == PlaylistShare.account_id)
                      .where(PlaylistShare.playlist_id == playlist.id).order_by(Account.name)).all()
    people = db.scalars(select(Account).where(Account.role != OPERATOR, Account.blocked_at.is_(None))
                        .order_by(Account.display_name, Account.name)).all()
    return {
        "shares": [{"person": person(account), "access": share.access} for share, account in rows],
        "people": [person(account) for account in people if account.id != playlist.owner_id],
    }


def share(db: Session, account: Account, playlist_id: int, other_id: int, access: str) -> dict[str, Any]:
    if access not in (SHARE_READ, SHARE_EDIT):
        raise error("invalid_input", "The input is not valid.", 422, fields=["access"])
    playlist, _level = _begin(db, account, playlist_id, "share")
    other = db.get(Account, other_id)
    if other is None or other.blocked_at is not None:
        db.rollback()
        raise error("not_found", "Not found.", 404)
    if other.id == playlist.owner_id or other.role == OPERATOR:
        db.rollback()
        raise error("share_needless", "This person sees the playlist anyway.", 422)
    row = db.get(PlaylistShare, (playlist_id, other_id))
    if row is None:
        db.add(PlaylistShare(playlist_id=playlist_id, account_id=other_id, access=access, created_at=clock.now()))
    elif row.access == access:
        db.rollback()
        return shares_view(db, playlist)
    else:
        row.access = access
    db.commit()
    logger.info("Playlist shared id=%s access=%s", playlist_id, access)
    _publish(playlist_id, "access", {"version": playlist.version})
    return shares_view(db, playlist)


def unshare(db: Session, account: Account, playlist_id: int, other_id: int) -> dict[str, Any]:
    playlist, _level = _begin(db, account, playlist_id, "share")
    gone = db.execute(delete(PlaylistShare).where(PlaylistShare.playlist_id == playlist_id,
                                                  PlaylistShare.account_id == other_id)).rowcount
    if not gone:
        db.rollback()
        return shares_view(db, playlist)
    db.commit()
    logger.info("Playlist no longer shared id=%s", playlist_id)
    _publish(playlist_id, "access", {"version": playlist.version})
    return shares_view(db, playlist)


def ceil_days(seconds: float) -> int:
    return max(0, math.ceil(seconds / 86400))
