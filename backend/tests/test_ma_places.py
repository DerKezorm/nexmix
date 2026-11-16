"""Places 0, negative and past the end never reach Music Assistant: the check in ``_take_out`` itself (the readings
before a step always give 1 to n, so only a direct call reaches it). And a playlist Music Assistant still gives, though
no longer as one of its own, is never taken for deleted."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.music_assistant import MaError
from app.services.sources import music_assistant as connector

from .fakema import FakeMA
from .ma_world import connect


@pytest.mark.parametrize("places", [[0], [-1], [1, 0], [4], [2, 9]])
def test_bad_places_never_go_out(client: TestClient, operator: object, fake_ma: FakeMA, places: list[int]) -> None:
    server = connect(client, fake_ma)
    from app.db import SessionLocal
    from app.services import media, sources

    key = str(fake_ma.ui_create("Orte", sorted(fake_ma.tracks)[:3]))
    with SessionLocal() as db:
        row = media.get(db, server)
        source = sources.open_server(db, row)
    before = len(fake_ma.calls)
    with pytest.raises(MaError) as caught:
        source._take_out(key, places, 3)
    assert caught.value.code == "ma_bad_place"
    assert not [one for one in fake_ma.calls[before:] if one[0] == "music/playlists/remove_playlist_tracks"]
    assert isinstance(source, connector.MusicAssistantSource)


def test_a_playlist_music_assistant_still_gives_but_not_as_its_own_is_not_gone(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    server = connect(client, fake_ma)
    from app.db import SessionLocal
    from app.services import media, sources

    ident = fake_ma.ui_create("Ohne Zuordnung", sorted(fake_ma.tracks)[:2])
    original = fake_ma._playlist

    def unmapped(playlist: object) -> dict[str, object]:
        found = original(playlist)  # type: ignore[arg-type]
        if found["item_id"] == str(ident):
            found["provider_mappings"] = []
        return found

    monkeypatch.setattr(fake_ma, "_playlist", unmapped)
    with SessionLocal() as db:
        source = sources.open_server(db, media.get(db, server))
    assert source.gone(str(ident)) is False
    fake_ma.ui_delete(ident)
    assert source.gone(str(ident)) is True


def test_a_playlist_no_longer_given_as_its_own_stays_out_of_the_trash(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """Taken in from Music Assistant, then listed no longer as one of its own (the mapping gone): it stays in nexmix,
    out of the trash, and its sync says it cannot be read."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Playlist
    from app.services import sync as server_sync

    from .ma_world import events, sync

    server = connect(client, fake_ma)
    ident = fake_ma.ui_create("Bald ohne Zuordnung", sorted(fake_ma.tracks)[:2])
    sync(look=True)
    with SessionLocal() as db:
        taken = list(db.scalars(select(Playlist).where(Playlist.name == "Bald ohne Zuordnung")))
    assert len(taken) == 1
    original = fake_ma._playlist

    def unmapped(playlist: object) -> dict[str, object]:
        found = original(playlist)  # type: ignore[arg-type]
        if found["item_id"] == str(ident):
            found["provider_mappings"] = []
        return found

    monkeypatch.setattr(fake_ma, "_playlist", unmapped)
    for _round in range(3):
        with SessionLocal() as db:
            server_sync.request_all(db, server)
        events(server, fake_ma)
        sync(look=True)
    with SessionLocal() as db:
        row = db.get(Playlist, taken[0].id)
        assert row is not None and row.deleted_at is None
