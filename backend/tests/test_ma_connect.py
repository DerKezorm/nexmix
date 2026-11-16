"""Connecting Music Assistant: address and token, the providers, the catalogue, what the card says (the token's end),
and what never leaves the server (the token, the address, Music Assistant's ids)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import CatalogAlbum, CatalogTrack, MediaServer, TrackTag
from app.services import catalog, ma_link, media, music_assistant

from .fakema import FILES, OTHER_FILES, PLEX, FakeMA
from .ma_world import ADDRESS, connect, row


def test_connecting_asks_for_the_providers_then_fetches_the_catalogue(client: TestClient, operator: object,
                                                                       fake_ma: FakeMA) -> None:
    answer = client.post("/api/v1/music-assistant/connect", json={"url": ADDRESS, "token": fake_ma.token})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    # Three music providers offered (the built-in one never), none chosen yet: no catalogue.
    assert sorted(one["name"] for one in found["providers"]) == ["Hörbuch-Ordner", "Musik", "Plex"]
    assert not any(one["chosen"] for one in found["providers"])
    assert found["server"]["ready"] is False and found["server"]["library"] is None
    # When the token runs out: a year from now.
    days = found["server"]["token"]["days_left"]
    assert 363 <= days <= 365 and found["server"]["token"]["warn"] is False
    server = int(found["server"]["id"])
    handles = {one["name"]: one["handle"] for one in found["providers"]}
    chosen = client.put(f"/api/v1/music-assistant/{server}/providers", json={"handles": [handles["Musik"]]})
    assert chosen.status_code == 200
    assert chosen.json()["server"]["library"] == "Musik"
    from app.services import jobs

    assert jobs.run_pending(lane="catalog") >= 1
    with SessionLocal() as db:
        generation = media.generation_of(db, server)
        tracks = db.scalars(select(CatalogTrack).where(CatalogTrack.server_id == server,
                                                       CatalogTrack.generation == generation)).all()
        albums = db.scalars(select(CatalogAlbum).where(CatalogAlbum.server_id == server,
                                                       CatalogAlbum.generation == generation)).all()
        genres = db.scalars(select(TrackTag).where(TrackTag.server_id == server)).all()
    # Only the tracks of the chosen provider: not the one of the other folder.
    titles = {one.title for one in tracks}
    assert "Anfang" not in titles and "Small Hours" in titles
    assert len(tracks) == sum(1 for one in fake_ma.tracks.values() if one.mappings[0][0] == FILES)
    # The note Music Assistant took out of the name is back in the title, "feat." stays out as the performer.
    assert "Glasshouse (Live at the Pier)" in titles
    copper = next(one for one in tracks if one.title == "Copper Sky")
    assert copper.artist == "Mira Solenne, Dax Orlo"
    # The album's year and picture; genres by Music Assistant's genre lists.
    assert all(one.year for one in albums) and all(len(one.thumb) == 64 for one in albums)
    assert genres
    assert row().providers == [FILES]


def test_a_bad_token_or_no_music_assistant_is_said(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    wrong = client.post("/api/v1/music-assistant/connect", json={"url": ADDRESS, "token": "x.y.z"})
    assert wrong.status_code == 422 and wrong.json()["detail"]["code"] == "bad_token"
    gone = fake_ma.make_token(-1)
    expired = client.post("/api/v1/music-assistant/connect", json={"url": ADDRESS, "token": gone})
    assert expired.status_code == 422 and expired.json()["detail"]["code"] == "bad_token"
    fake_ma.schema = 20
    old = client.post("/api/v1/music-assistant/connect", json={"url": ADDRESS, "token": fake_ma.token})
    assert old.status_code == 422 and old.json()["detail"]["code"] == "ma_too_old"
    fake_ma.schema = 65
    fake_ma.down = True
    away = client.post("/api/v1/music-assistant/connect", json={"url": ADDRESS, "token": fake_ma.token})
    assert away.status_code == 502 and away.json()["detail"]["code"] == "ma_unreachable"
    metadata = client.post("/api/v1/music-assistant/connect",
                           json={"url": "http://metadata.example.com:8095", "token": fake_ma.token})
    assert metadata.status_code == 422


def test_nothing_of_music_assistant_leaves_the_server(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    texts = [client.get(path).text for path in ("/api/v1/music-assistant", f"/api/v1/music-assistant/{server}",
                                                f"/api/v1/music-assistant/{server}/providers", "/api/v1/servers",
                                                f"/api/v1/music-assistant/{server}/playlists")]
    for text in texts:
        assert fake_ma.token not in text and "ma.example.com" not in text and FILES not in text
        assert PLEX not in text and fake_ma.server_id not in text
    with SessionLocal() as db:
        stored = db.get(MediaServer, server)
        assert stored is not None and fake_ma.token not in stored.token_enc
    from app.services import logs

    assert fake_ma.token not in logs.log_file().read_text(encoding="utf-8")


def test_a_plex_provider_beside_plex_is_named_twice(client: TestClient, operator: object, fake_ma: FakeMA,
                                                    fake_plex: object) -> None:
    from .plex_world import connect as connect_plex

    connect_plex(client, fake_plex)  # type: ignore[arg-type]
    server = connect(client, fake_ma, providers=(FILES, PLEX))
    found = client.get(f"/api/v1/music-assistant/{server}/providers").json()
    assert found["doubled"] == ["Plex"]


def test_the_card_warns_thirty_days_before_the_token_runs_out(client: TestClient, operator: object,
                                                              fake_ma: FakeMA) -> None:
    fake_ma.token = fake_ma.make_token(20)
    server = connect(client, fake_ma)
    token = client.get(f"/api/v1/music-assistant/{server}").json()["token"]
    assert token["warn"] is True and token["expired"] is False and 19 <= token["days_left"] <= 20
    with SessionLocal() as db:
        found = db.get(MediaServer, server)
        assert found is not None
        later = ma_link.token_view(found, clock.now() + timedelta(days=21))
    assert later["expired"] is True


def test_other_providers_fetch_the_catalogue_anew(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    handles = [ma_link.handle(fake_ma.server_id, one) for one in (FILES, OTHER_FILES)]
    assert client.put(f"/api/v1/music-assistant/{server}/providers", json={"handles": handles}).status_code == 200
    from app.services import jobs

    jobs.run_pending(lane="catalog")
    with SessionLocal() as db:
        generation = media.generation_of(db, server)
        titles = set(db.scalars(select(CatalogTrack.title).where(CatalogTrack.server_id == server,
                                                                 CatalogTrack.generation == generation)))
    assert "Anfang" in titles
    unknown = client.put(f"/api/v1/music-assistant/{server}/providers", json={"handles": ["0" * 16]})
    assert unknown.status_code == 404


def test_a_partial_run_compares_the_totals(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    with SessionLocal() as db:
        assert catalog.request_partial(db, "schedule", server)
    from app.services import jobs

    jobs.run_pending(lane="catalog")
    with SessionLocal() as db:
        before = media.generation_of(db, server)
    fake_ma.add_track("Lumen Vale", "Quiet Engines", 2022, "Second Hour", 201, number=2)
    with SessionLocal() as db:
        assert catalog.request_partial(db, "schedule", server)
    jobs.run_pending(lane="catalog")
    jobs.run_pending(lane="catalog")
    with SessionLocal() as db:
        after = media.generation_of(db, server)
        titles = set(db.scalars(select(CatalogTrack.title).where(CatalogTrack.server_id == server,
                                                                 CatalogTrack.generation == after)))
    assert after == before + 1 and "Second Hour" in titles


def test_the_operator_alone_connects(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    from .conftest import person

    with person("Mia") as other:
        assert other.get("/api/v1/music-assistant").status_code == 403
        assert other.post("/api/v1/music-assistant/connect",
                          json={"url": ADDRESS, "token": fake_ma.token}).status_code == 403


def test_the_websocket_is_one_per_server(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    # The sign-in tried one of its own (closed again), the catalogue the server's shared one.
    open_ones = [one for one in fake_ma.channels if not one.closed]
    assert len(open_ones) == 1
    assert music_assistant.current(server) is not None


# --- Ready for the internet? ------------------------------------------------------------------------------------------


def _point(client: TestClient) -> dict[str, object] | None:
    points = client.get("/api/v1/settings/readiness").json()["points"]
    return next((point for point in points if point["key"] == "music_assistant"), None)


def test_ready_for_the_internet_looks_at_music_assistant(client: TestClient, operator: object,
                                                         fake_ma: FakeMA) -> None:
    assert _point(client) is None
    connect(client, fake_ma)
    point = _point(client)
    # Reached at a public address: a warning (it serves previews and pictures without signing in).
    assert point is not None and point["state"] == "warn" and point["values"] == {
        "names": "Music Assistant", "token": True, "days": None}


def test_music_assistant_inside_the_own_network_is_ready_until_its_token_runs_out(
        client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    connect(client, fake_ma, url="http://127.0.0.1:8095")
    point = _point(client)
    assert point is not None and point["state"] == "ok"
    with SessionLocal() as db:
        found = db.get(MediaServer, row().id)
        assert found is not None
        found.token_expires_at = clock.now() + timedelta(days=10)
        db.commit()
    point = _point(client)
    assert point is not None and point["state"] == "warn" and point["values"]["days"] in (9, 10)
    with SessionLocal() as db:
        found = db.get(MediaServer, row().id)
        assert found is not None
        found.token_expires_at = clock.now() - timedelta(days=1)
        db.commit()
    point = _point(client)
    assert point is not None and point["state"] == "bad"
