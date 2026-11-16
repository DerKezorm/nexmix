"""Plex and Jellyfin with the same made-up music, for the tests of mirrors, copying and moving: Jellyfin gets the tracks
of the stand-in Plex's library (``twin``), but some (``leave_out``), and keeps its own as well, so that a playlist of one
server finds most of its tracks on the other, a few nowhere, and the other server has tracks of its own."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import MirrorExtra, MirrorItem, ServerPlaylist

from .fakejellyfin import FakeJellyfin
from .fakejellyfin.twins import twin as twin_items
from .fakeplex import FakePlex
from .jellyfin_world import connect as connect_jellyfin
from .jellyfin_world import key_of, number_of
from .plex_world import connect as connect_plex
from .plex_world import keys_of, local_tracks, numbers_of


def twin(jellyfin: FakeJellyfin, plex_fake: FakePlex, leave_out: frozenset[int] = frozenset(),
         retitled: dict[int, str] | None = None) -> dict[int, str]:
    """Jellyfin gets the tracks of the stand-in Plex's library but ``leave_out`` (``fakejellyfin/twins.py``)."""
    return twin_items(jellyfin, plex_fake.items, leave_out, retitled)


def connect_both(client: TestClient, plex_fake: FakePlex, jellyfin: FakeJellyfin,
                 leave_out: frozenset[int] = frozenset()) -> tuple[int, int, dict[int, str]]:
    """Plex (server 1) and Jellyfin with the twin library, both with their catalogue. Returns both servers and the
    twins."""
    connect_plex(client, plex_fake)
    twins = twin(jellyfin, plex_fake, leave_out)
    jelly = connect_jellyfin(client, jellyfin)
    return 1, jelly, twins


def plex_numbers(keys: list[int]) -> list[int]:
    return numbers_of(keys, 1)


def jelly_numbers(server: int, ids: list[str]) -> list[int]:
    return [number_of(server, one) for one in ids]


def copy_row(playlist_id: int, server: int) -> ServerPlaylist:
    with SessionLocal() as db:
        row = db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == playlist_id,
                                                      ServerPlaylist.server_id == server)).one()
        db.expunge(row)
    return row


def mirror_key(playlist_id: int, server: int) -> str:
    key = copy_row(playlist_id, server).ext_key
    assert key is not None
    return key


def mapping(playlist_id: int, server: int) -> dict[int, tuple[int | None, str, str]]:
    """Entry → (the mirror's track, how, hint)."""
    row = copy_row(playlist_id, server)
    with SessionLocal() as db:
        return {int(one.item_id): (one.track_id, one.how, one.hint)
                for one in db.scalars(select(MirrorItem).where(MirrorItem.copy_id == row.id))}


def extras(playlist_id: int, server: int) -> list[MirrorExtra]:
    row = copy_row(playlist_id, server)
    with SessionLocal() as db:
        found = list(db.scalars(select(MirrorExtra).where(MirrorExtra.copy_id == row.id).order_by(MirrorExtra.id)))
        for one in found:
            db.expunge(one)
    return found


def jelly_tracks_of(jellyfin: FakeJellyfin, playlist_id: int, server: int) -> list[str]:
    return list(dict.fromkeys(jellyfin.tracks_of(mirror_key(playlist_id, server))))


def plex_titles(plex_fake: FakePlex, playlist_id: int) -> list[str]:
    """The titles of the playlist's copy on Plex (server 1), source or mirror."""
    return [plex_fake.items[key].title for key in plex_fake.lists.tracks(int(mirror_key(playlist_id, 1)))]


def jelly_titles(jellyfin: FakeJellyfin, playlist_id: int, server: int) -> list[str]:
    return [jellyfin.tracks[one].name for one in jelly_tracks_of(jellyfin, playlist_id, server)]


def local_titles(playlist_id: int) -> list[str]:
    from .plex_world import local_items

    return [item.title for item in local_items(playlist_id)]


def plex_tracks_here(playlist_id: int) -> list[int]:
    """The entries by Plex's keys (entries of another server left out)."""
    with SessionLocal() as db:
        from app.services import media

        keys = media.keys(db, 1, media.TRACK, local_tracks(playlist_id))
    return [int(keys[track]) for track in local_tracks(playlist_id) if track in keys]


def look(client: TestClient, server: int, body: dict[str, Any]) -> dict[str, Any]:
    """The preview of "take all over" to ``server``, begun as the interface does and read at its end."""
    from app.services import mirrors

    begun = client.post(f"/api/v1/servers/{server}/transfer/preview", json=body)
    assert begun.status_code == 202, begun.text
    assert begun.json()["preview"]["state"] in ("running", "done")
    mirrors.wait_look(server)
    answer = client.get(f"/api/v1/servers/{server}/transfer/preview")
    assert answer.status_code == 200, answer.text
    state = answer.json()["preview"]
    assert state["state"] == "done" and state["done"] == state["total"], state
    return state["result"]


def detail(client: TestClient, playlist_id: int) -> dict[str, Any]:
    answer = client.get(f"/api/v1/playlists/{playlist_id}")
    assert answer.status_code == 200, answer.text
    return answer.json()


__all__ = [
    "connect_both",
    "copy_row",
    "detail",
    "extras",
    "jelly_numbers",
    "jelly_titles",
    "jelly_tracks_of",
    "key_of",
    "keys_of",
    "local_titles",
    "look",
    "mapping",
    "mirror_key",
    "plex_numbers",
    "plex_titles",
    "plex_tracks_here",
    "twin",
]
