"""Covers and previews of Music Assistant, passed through nexmix: only a checked picture id and a fixed size reach it,
a preview's token never reaches a browser, and the same track on another server gives its preview from 35 %."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import CatalogAlbum
from app.services import media

from .fakema import FakeMA
from .fakeplex import FakePlex
from .ma_world import connect, number_of


def _album(server: int, name: str) -> int:
    with SessionLocal() as db:
        generation = media.generation_of(db, server)
        found = db.scalar(select(CatalogAlbum.id).where(CatalogAlbum.server_id == server,
                                                        CatalogAlbum.generation == generation,
                                                        CatalogAlbum.title == name))
    assert found is not None
    return int(found)


def test_a_cover_comes_at_music_assistants_own_size_as_jpeg(client: TestClient, operator: object,
                                                            fake_ma: FakeMA) -> None:
    fake_ma.add_track("Ohne Bild", "Kahl", 2020, "Leer", 100, picture=False)
    server = connect(client, fake_ma)
    fake_ma.http.clear()
    answer = client.get(f"/api/v1/catalog/cover/{_album(server, 'Copper Sky')}?s=200")
    assert answer.status_code == 200 and answer.headers["content-type"] == "image/jpeg"
    [(path, params)] = fake_ma.http
    assert path.startswith("/imageproxy/") and len(path.rsplit("/", 1)[1]) == 64
    assert params == {"size": "256", "fmt": "jpeg"}
    # Kept on disk: the second time Music Assistant is not asked.
    assert client.get(f"/api/v1/catalog/cover/{_album(server, 'Copper Sky')}?s=200").status_code == 200
    assert len(fake_ma.http) == 1
    # An album without a picture: 404, Music Assistant not asked; a size nexmix does not give: 422.
    assert client.get(f"/api/v1/catalog/cover/{_album(server, 'Kahl')}?s=80").status_code == 404
    assert client.get(f"/api/v1/catalog/cover/{_album(server, 'Copper Sky')}?s=256").status_code == 422
    assert len(fake_ma.http) == 1


def test_a_preview_of_music_assistant_is_its_first_30_seconds(client: TestClient, operator: object,
                                                               fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    track = number_of(server, fake_ma.track_by_name("Small Hours").id)
    answer = client.get(f"/api/v1/preview/{track}")
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "audio/aac" and answer.headers["cache-control"] == "no-store"
    assert answer.content[:2] == b"\xff\xf1"
    # The token went from Music Assistant to nexmix only.
    tokens = list(fake_ma.previews)
    assert tokens and all(token not in answer.text and token not in str(answer.headers) for token in tokens)
    assert fake_ma.preview_starts == [fake_ma.track_by_name("Small Hours").id]
    # Safari asks for parts: the kept preview answers them, Music Assistant is not asked again.
    first = client.get(f"/api/v1/preview/{track}", headers={"Range": "bytes=0-1"})
    assert first.status_code in (200, 206)
    second = client.get(f"/api/v1/preview/{track}", headers={"Range": "bytes=2-"})
    assert second.status_code == 206 and second.headers["content-type"] == "audio/aac"
    assert len(fake_ma.preview_starts) == 1


def test_the_same_track_on_plex_gives_its_preview_from_35_percent(client: TestClient, operator: object,
                                                                  fake_ma: FakeMA, fake_plex: FakePlex) -> None:
    from .plex_world import connect as connect_plex

    connect_plex(client, fake_plex)
    server = connect(client, fake_ma)
    track = number_of(server, fake_ma.track_by_name("Fever Glass").id)
    answer = client.get(f"/api/v1/preview/{track}")
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "audio/mpeg"
    assert fake_ma.preview_starts == []
    [asked] = fake_plex.starts
    assert int(asked["offset"]) == int(200 * 0.35)


def test_music_assistant_away_says_so(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    track = number_of(server, fake_ma.track_by_name("Small Hours").id)
    fake_ma.restart()
    from app.services import music_assistant

    music_assistant.forget()
    answer = client.get(f"/api/v1/preview/{track}")
    assert answer.status_code in (502, 503) and answer.json()["detail"]["code"] == "ma_unreachable"
