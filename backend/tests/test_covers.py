"""Covers through the server: only for the signed-in, only paths the catalogue took from Plex, exactly five parameters
to Plex, kept on disk, ETag and a long cache time."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import CatalogAlbum
from app.services import covers, jobs, plex

from .conftest import TAB, make_account, new_client
from .fakeplex import FakePlex


@pytest.fixture
def synced(client: TestClient, operator: object, fake_plex: FakePlex) -> FakePlex:
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake_plex.account_token})
    assert answer.status_code == 200
    jobs.run_pending(lane="catalog")
    fake_plex.calls.clear()
    return fake_plex


def photo_calls(fake: FakePlex) -> list[dict[str, str]]:
    return [params for _method, path, params in fake.library_calls() if path == "/photo/:/transcode"]


@pytest.mark.parametrize("size", [80, 200, 400])
def test_a_cover_asks_plex_with_exactly_five_parameters(client: TestClient, synced: FakePlex, size: int) -> None:
    answer = client.get(f"/api/v1/catalog/cover/2001?s={size}")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "image/jpeg"
    assert answer.content[:2] == b"\xff\xd8"
    sent = photo_calls(synced)
    assert len(sent) == 1
    assert set(sent[0]) == {"width", "height", "minSize", "upscale", "url"}
    assert sent[0] == {"width": str(size), "height": str(size), "minSize": "1", "upscale": "1",
                       "url": sent[0]["url"]}
    assert sent[0]["url"].startswith("/library/metadata/2001/thumb/")


def test_a_cover_is_kept_and_served_again_without_plex(client: TestClient, synced: FakePlex) -> None:
    first = client.get("/api/v1/catalog/cover/2001?s=80")
    second = client.get("/api/v1/catalog/cover/2001?s=80")
    assert first.content == second.content
    assert len(photo_calls(synced)) == 1
    assert first.headers["etag"] == second.headers["etag"]
    assert "max-age=604800" in first.headers["cache-control"] and "private" in first.headers["cache-control"]
    unchanged = client.get("/api/v1/catalog/cover/2001?s=80", headers={"If-None-Match": first.headers["etag"]})
    assert unchanged.status_code == 304 and unchanged.content == b""
    assert len(list(covers.folder().glob("*/*.img"))) == 1


def test_a_changed_cover_at_plex_is_a_new_file(client: TestClient, synced: FakePlex) -> None:
    first = client.get("/api/v1/catalog/cover/2001?s=80").headers["etag"]
    with SessionLocal() as db:
        db.execute(update(CatalogAlbum).where(CatalogAlbum.id == 2001).values(thumb="/library/metadata/2001/thumb/99"))
        db.commit()
    second = client.get("/api/v1/catalog/cover/2001?s=80")
    assert second.headers["etag"] != first
    assert len(photo_calls(synced)) == 2


def test_only_the_three_sizes(client: TestClient, synced: FakePlex) -> None:
    for size in ("81", "1000", "0", "-1", "abc"):
        assert client.get(f"/api/v1/catalog/cover/2001?s={size}").status_code == 422, size
    assert photo_calls(synced) == []


def test_an_unknown_album_or_one_without_cover_is_not_found(client: TestClient, synced: FakePlex) -> None:
    assert client.get("/api/v1/catalog/cover/2999").status_code == 404
    with SessionLocal() as db:
        db.execute(update(CatalogAlbum).where(CatalogAlbum.id == 2002).values(thumb=""))
        db.commit()
    assert client.get("/api/v1/catalog/cover/2002").status_code == 404
    assert photo_calls(synced) == []


def test_a_path_that_is_no_cover_never_reaches_plex(client: TestClient, synced: FakePlex) -> None:
    for bad in ("/library/metadata/2001/file", "/:/prefs", "http://169.254.169.254/latest",
                "/library/metadata/2001/thumb/1?format=webp", "/library/metadata/2001/thumb/../../x"):
        with SessionLocal() as db:
            db.execute(update(CatalogAlbum).where(CatalogAlbum.id == 2001).values(thumb=bad))
            db.commit()
        assert client.get("/api/v1/catalog/cover/2001").status_code == 404, bad
    assert photo_calls(synced) == []
    with pytest.raises(plex.PlexError):
        plex.PlexServer("http://127.0.0.1:32400", "t", "c").cover("/library/metadata/1/thumb/1?format=webp", 80)


@pytest.mark.parametrize("ending", ["\n", "\r\n", " ", "\n/library/metadata/1/thumb/1"])
def test_a_cover_path_with_anything_after_it_is_no_cover(client: TestClient, synced: FakePlex, ending: str) -> None:
    good = "/library/metadata/2001/thumb/1"
    assert plex.THUMB.fullmatch(good) and plex.THUMB.match(good)
    assert not plex.THUMB.match(good + ending)
    assert not plex.KEY.match("3" + ending)
    with SessionLocal() as db:
        db.execute(update(CatalogAlbum).where(CatalogAlbum.id == 2001).values(thumb=good + ending))
        db.commit()
    assert client.get("/api/v1/catalog/cover/2001").status_code == 404
    assert photo_calls(synced) == []
    with pytest.raises(plex.PlexError):
        plex.PlexServer("http://127.0.0.1:32400", "t", "c").cover(good + ending, 80)


def test_a_cover_path_from_plex_with_a_line_break_is_not_taken(client: TestClient, operator: object,
                                                               fake_plex: FakePlex,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    entry = fake_plex.entry

    def broken(item: object, *, full: bool = False, light: bool = False) -> dict[str, object]:
        out = entry(item, full=full, light=light)  # type: ignore[arg-type]
        if out.get("ratingKey") == "2003":
            out["thumb"] = "/library/metadata/2003/thumb/1\n"
        return out

    monkeypatch.setattr(fake_plex, "entry", broken)
    client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake_plex.account_token})
    jobs.run_pending(lane="catalog")
    with SessionLocal() as db:
        album = db.scalars(select(CatalogAlbum).where(CatalogAlbum.id == 2003)).first()
        assert album is not None and album.thumb == ""
    assert client.get("/api/v1/catalog/cover/2003?s=80").status_code == 404
    assert client.get("/api/v1/catalog/cover/2004?s=80").status_code == 200


def test_without_a_sign_in_there_is_no_cover(synced: FakePlex) -> None:
    from app.main import app

    with TestClient(app, base_url="http://testserver", headers=TAB) as stranger:
        assert stranger.get("/api/v1/catalog/cover/2001").status_code == 401
        assert stranger.get("/api/v1/catalog/status").status_code == 401
    assert photo_calls(synced) == []


def test_a_member_gets_covers(synced: FakePlex) -> None:
    with new_client(make_account("mila")) as browser:
        assert browser.get("/api/v1/catalog/cover/2005?s=200").status_code == 200


def test_plex_sending_something_else_than_a_picture_is_refused(client: TestClient, synced: FakePlex,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    def svg(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<svg onload=alert(1)>", headers={"content-type": "image/svg+xml"})

    monkeypatch.setattr(plex, "TRANSPORT", httpx.MockTransport(svg))
    assert client.get("/api/v1/catalog/cover/2001").status_code == 404
    assert list(covers.folder().glob("*/*.img")) == []


def test_a_cover_too_large_is_refused(client: TestClient, synced: FakePlex, monkeypatch: pytest.MonkeyPatch) -> None:
    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8" + b"0" * (plex.COVER_BYTES + 1),
                              headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(plex, "TRANSPORT", httpx.MockTransport(huge))
    assert client.get("/api/v1/catalog/cover/2001").status_code == 404


def test_plex_out_of_reach_says_so(client: TestClient, synced: FakePlex) -> None:
    synced.down.add("127.0.0.1")
    answer = client.get("/api/v1/catalog/cover/2001")
    assert answer.status_code == 503 and answer.json()["detail"]["code"] == "plex_unreachable"
    assert int(answer.headers["retry-after"]) >= 1


def test_the_kept_covers_stay_under_their_limit(client: TestClient, synced: FakePlex,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "cover_cache_mb", 1)
    for number in range(30):
        covers.write(f"{number:032x}", b"\xff\xd8" + b"1" * 60_000)
    covers.prune()
    total = sum(path.stat().st_size for path in covers.folder().glob("*/*.img"))
    assert total <= 1_000_000
    assert len(list(covers.folder().glob("*/*.img"))) < 30


def test_disconnecting_takes_the_kept_covers_along(client: TestClient, synced: FakePlex) -> None:
    client.get("/api/v1/catalog/cover/2001?s=80")
    assert list(covers.folder().glob("*/*.img"))
    client.delete("/api/v1/plex")
    assert not covers.folder().exists()
    assert client.get("/api/v1/catalog/cover/2001?s=80").status_code == 404
