"""Entries that lost their track when the catalogue of their server changed, found again.

Jellyfin's id of a track hangs on the path of its file: renamed or moved, the file is a track gone and a new one, and
Jellyfin drops it from its own playlists without a word. nexmix keeps every entry with what it knew of its track (title,
artist, album, length), so after each run of the catalogue of a Jellyfin server the entries of the playlists that take
their tracks from it and whose track is no longer in the catalogue are looked for (``match.py``, only what says the
same file: the same id of the recording, or the same title on the same album by the same artist, ``SAME``). Found,
the entry takes the new track and keeps its place, its time and who put it in. Not found, it stays as it is: the
interface marks it as missing from the library, as for Plex.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import clock
from ..models import SERVER_JELLYFIN, CatalogTrack, Playlist, PlaylistItem
from . import match, media

if TYPE_CHECKING:
    from .sync import _Catalogue

logger = logging.getLogger("nexmix.relink")

#: How sure a track found must be to stand for the one gone: a renamed or moved file keeps its tags, so the same id of
#: the recording, or the same title on the same album by the same artist. The same title by the same artist on another
#: album (``match.LENGTH``) is another version of the song (a remaster, a best-of), never the file renamed.
SAME = frozenset({match.GUID, match.MBID, match.ALBUM})


def after_run(db: Session, server_id: int) -> int:
    """After a run of the catalogue of ``server_id``: the entries whose track is gone, found again where that is sure.
    Returns how many were found."""
    from . import playlists, sync

    row = media.get(db, server_id)
    if row is None or row.kind != SERVER_JELLYFIN or not row.active or not row.generation:
        db.rollback()
        return 0
    generation = int(row.generation)
    owned = list(db.scalars(select(Playlist.id).where(Playlist.source_server_id == server_id)
                            .order_by(Playlist.id)))
    db.rollback()
    if not owned:
        return 0
    live = set(db.scalars(select(CatalogTrack.id).where(CatalogTrack.server_id == server_id,
                                                        CatalogTrack.generation == generation)))
    db.rollback()
    found_in_all = 0
    catalogue = None
    for playlist_id in owned:
        items = playlists.live_items(db, playlist_id)
        db.rollback()
        if not any(item.track_id not in live for item in items):
            continue
        if catalogue is None:
            # A partial run keeps the generation: the catalogue kept for it may be older than this run.
            sync._whole.pop(server_id, None)
            catalogue = sync._catalogue(db, server_id)
            db.rollback()
        found_in_all += _relink(db, playlist_id, server_id, catalogue)
    return found_in_all


def _relink(db: Session, playlist_id: int, server_id: int, catalogue: _Catalogue) -> int:
    from . import playlists, sync

    # The write lock first: nothing changes the entries while they are looked at.
    taken = db.execute(update(Playlist).where(Playlist.id == playlist_id, Playlist.source_server_id == server_id)
                       .values(version=Playlist.version + 1, updated_at=clock.now())).rowcount
    if not taken:
        db.rollback()
        return 0
    items = playlists.live_items(db, playlist_id)
    lost = [item for item in items if item.track_id not in catalogue.by_id]
    if not lost:
        db.rollback()
        return 0
    used = {item.track_id for item in items if item.track_id in catalogue.by_id}
    moving: dict[int, match.Candidate] = {}
    for item in lost:
        hit = catalogue.find(sync._wanted(item), used)
        if hit is not None and hit.how in SAME:
            used.add(hit.track.id)
            moving[item.id] = hit.track
    if not moving:
        db.rollback()
        return 0
    by_id = {item.id: item for item in items}
    # Two steps: one track once per playlist, even for a moment.
    for ident in moving:
        by_id[ident].track_id = -ident
    db.flush()
    for ident, track in moving.items():
        item: PlaylistItem = by_id[ident]
        item.track_id, item.album_id = track.id, catalogue.albums.get(track.id, 0)
    db.commit()
    logger.info("Playlist entries found again after the catalogue changed playlist=%s server=%s found=%s missing=%s",
                playlist_id, server_id, len(moving), len(lost) - len(moving))
    sync.publish_items(db, playlist_id, [], changed=[by_id[ident] for ident in moving])
    return len(moving)
