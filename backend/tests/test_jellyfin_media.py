"""Covers and previews of a Jellyfin server, against the stand-in: the picture of the album as JPEG through nexmix's
store, the sound from ``stream.mp3`` 35 % in at 192 kbit/s with the token in a header, the parts Safari asks for, an
album without a picture, and a Jellyfin in trouble keeping nobody else's covers."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, CatalogAlbum
from app.services import covers, preview

from .fakejellyfin import FakeJellyfin
from .fakeplex.sound import FRAME
from .jellyfin_world import connect, key_of, number_of


@pytest.fixture
def jelly(client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> tuple[FakeJellyfin, int]:
    return fake_jellyfin, connect(client, fake_jellyfin)


def album_number(server: int, title: str) -> int:
    with SessionLocal() as db:
        return int(db.scalars(select(CatalogAlbum.id).where(CatalogAlbum.server_id == server,
                                                            CatalogAlbum.title == title)).one())


def pictures(fake: FakeJellyfin) -> list[dict[str, str]]:
    return [params for _method, path, params in fake.calls if path.endswith("/Images/Primary")]


def test_a_cover_comes_from_the_album_as_jpeg_and_then_from_the_store(client: TestClient,
                                                                      jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    album = album_number(server, "Wolkenfabrik")
    answer = client.get(f"/api/v1/catalog/cover/{album}?s=200")
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "image/jpeg" and answer.content[:2] == b"\xff\xd8"
    [asked] = pictures(fake)
    # Always JPEG (a browser's Accept would get WebP), the size asked for, nothing else.
    assert asked == {"fillWidth": "200", "fillHeight": "200", "quality": "80", "format": "Jpg"}
    path = next(path for _method, path, _params in fake.calls if path.endswith("/Images/Primary"))
    assert path == f"/Items/{key_of(server, album, 'album')}/Images/Primary"
    again = client.get(f"/api/v1/catalog/cover/{album}?s=200")
    assert again.status_code == 200 and again.content == answer.content
    assert len(pictures(fake)) == 1, "the second time from nexmix's store"
    tagged = client.get(f"/api/v1/catalog/cover/{album}?s=200", headers={"If-None-Match": answer.headers["etag"]})
    assert tagged.status_code == 304
    # Neither the album's id at Jellyfin nor the address goes to the browser.
    assert key_of(server, album, "album") not in str(answer.headers)
    assert client.get(f"/api/v1/catalog/cover/{album}?s=300").status_code == 422


def test_an_album_without_a_picture_is_404_without_asking_jellyfin(client: TestClient,
                                                                   jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    fake.calls.clear()
    assert client.get(f"/api/v1/catalog/cover/{album_number(server, 'Hafenlieder')}?s=80").status_code == 404
    assert pictures(fake) == []
    assert client.get("/api/v1/catalog/cover/999999?s=80").status_code == 404


def test_a_jellyfin_in_trouble_is_left_alone_and_says_so_once(client: TestClient, jelly: tuple[FakeJellyfin, int],
                                                              caplog: pytest.LogCaptureFixture) -> None:
    fake, server = jelly
    fake.starting = 100
    caplog.set_level(logging.INFO, logger="nexmix.covers")
    first = client.get(f"/api/v1/catalog/cover/{album_number(server, 'Mondgarten')}?s=80")
    assert first.status_code == 503 and first.json()["detail"]["code"] == "jellyfin_starting"
    second = client.get(f"/api/v1/catalog/cover/{album_number(server, 'Neonstadt')}?s=80")
    assert second.status_code == 503 and int(second.headers["retry-after"]) >= 1
    assert len(pictures(fake)) == 1, "asked once, not for every cover"
    assert covers.quiet_for(server) > 0 and covers.quiet_for(server + 1) == 0, "other servers are not held up"
    loud = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(loud) == 1 and loud[0].startswith("Jellyfin does not deliver covers")


def test_a_preview_asks_stream_mp3_a_third_of_the_way_in(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    ident = next(key for key, one in fake.tracks.items() if one.name == "Kupferstrand" and one.seconds == 214)
    fake.sound_bytes = 200_000
    answer = client.get(f"/api/v1/preview/{number_of(server, ident)}")
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "audio/mpeg" and answer.headers["cache-control"] == "no-store"
    assert answer.content[:4] == FRAME[:4] and len(answer.content) == 200_000
    [asked] = fake.starts
    assert asked["startTimeTicks"] == str(int(214 * 0.35) * 10_000_000)
    assert asked["audioBitRate"] == "192000" and asked["static"] == "false"
    assert asked["deviceId"].startswith("nexmix-") and len(asked["playSessionId"]) == 24
    assert set(asked) == {"audioBitRate", "startTimeTicks", "static", "deviceId", "playSessionId"}
    path = next(path for _method, path, _params in fake.calls if path.endswith("/stream.mp3"))
    assert path == f"/Audio/{ident}/stream.mp3"
    assert ident not in str(answer.headers)
    assert preview.running_now() == 0


def test_a_preview_stops_at_one_megabyte(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    fake.sound_bytes = 3_000_000
    answer = client.get(f"/api/v1/preview/{number_of(server, next(iter(fake.tracks)))}")
    assert answer.status_code == 200 and len(answer.content) == preview.MAX_BYTES


def test_safari_asks_for_two_bytes_then_the_rest_and_jellyfin_is_asked_once(client: TestClient,
                                                                             jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    fake.sound_bytes = 300_000
    track = number_of(server, next(iter(fake.tracks)))
    first = client.get(f"/api/v1/preview/{track}", headers={"Range": "bytes=0-1"})
    assert first.status_code == 206 and first.headers["content-range"] == "bytes 0-1/300000"
    rest = client.get(f"/api/v1/preview/{track}", headers={"Range": "bytes=2-"})
    assert rest.status_code == 206 and len(rest.content) == 299_998
    assert len(fake.starts) == 1


def test_a_track_jellyfin_does_not_know_is_said_and_logged_without_the_token(
        client: TestClient, jelly: tuple[FakeJellyfin, int], caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch) -> None:
    fake, server = jelly
    waits: list[float] = []

    async def counted(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(preview, "pause", counted)
    gone = next(iter(fake.tracks))
    number = number_of(server, gone)
    fake.delete(gone)
    caplog.set_level(logging.INFO, logger="nexmix.preview")
    answer = client.get(f"/api/v1/preview/{number}")
    assert answer.status_code == 502 and answer.json()["detail"]["code"] == "jellyfin_error"
    lines = [record.getMessage() for record in caplog.records if record.name == "nexmix.preview"
             and record.levelno >= logging.WARNING]
    assert len(lines) == 2 and all(line.startswith("Jellyfin refused a preview") for line in lines)
    assert waits == [preview.RETRY_SECONDS], "a 400 may pass by itself: once more"
    for token in fake.tokens:
        assert all(token not in line for line in lines)
    assert all("jellyfin.example.com" not in line for line in lines)
