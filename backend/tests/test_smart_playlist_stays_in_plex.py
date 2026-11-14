"""A smart playlist of Plex is only read in nexmix: the interface offers no delete for it, and the interface (``/api/v1``)
must not either. Deleted through it, the sync would delete the smart playlist in Plex, and bringing it back makes an
ordinary one: Plex's rule is gone for good."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, PlexPlaylist
from app.services import plex_sync

from .fakeplex import FakePlex
from .plex_world import connect, sync


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    connect(client, fake_plex)
    plex_sync.forget()
    yield fake_plex


def test_a_smart_playlist_cannot_be_deleted_through_the_api(client: TestClient, world: FakePlex) -> None:
    smart = world.lists.make("Alles Neue", [3001, 3003, 3005], smart=True)
    world.tick()
    sync(world, look_at_plex=True)
    with SessionLocal() as db:
        playlist_id = db.scalar(select(PlexPlaylist.playlist_id).where(PlexPlaylist.rating_key == smart.key))
    assert playlist_id is not None
    answer = client.delete(f"/api/v1/playlists/{playlist_id}")
    sync(world, look_at_plex=True)
    assert smart.key in world.lists.lists and world.lists.lists[smart.key].smart, (
        "the smart playlist was deleted in Plex", answer.status_code)
