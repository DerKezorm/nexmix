"""What nobody but the operator reaches, and what never leaves the server, with Music Assistant: its token, its address,
its ids (of playlists, providers, previews) are in no answer, no event of a playlist and no line of the log; a member
without the playlist and a guest of another playlist reach nothing of it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services import events, logs

from .conftest import person
from .fakema import FILES, OTHER_FILES, FakeMA
from .ma_world import connect, list_key, make, numbers, sync
from .ma_world import events as ma_events


def _secrets(fake: FakeMA) -> list[str]:
    return [fake.token, "ma.example.com", fake.server_id, FILES, OTHER_FILES, *fake.previews]


def test_nothing_of_music_assistant_in_answers_events_or_the_log(client: TestClient, operator: object,
                                                                 fake_ma: FakeMA,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    told: list[Any] = []
    original = events.hub.publish

    def keep(topic: str, kind: str, payload: Any) -> None:
        told.append(payload)
        original(topic, kind, payload)

    monkeypatch.setattr(events.hub, "publish", keep)
    server = connect(client, fake_ma)
    tracks = numbers(server, [fake_ma.track_by_name(name).id for name in ("Northern Lights", "Small Hours")])
    made = make(client, server, "Geheim", tracks)
    sync()
    key = list_key(int(made["id"]))
    fake_ma.ui_add(key, fake_ma.track_by_name("Fever Glass").id)
    ma_events(server, fake_ma)
    sync()
    client.get(f"/api/v1/preview/{tracks[1]}")
    assert client.put(f"/api/v1/playlists/{made['id']}", json={"name": "Geheim 2"}).status_code == 200
    sync()
    answers = [client.get(path).text for path in (
        f"/api/v1/playlists/{made['id']}", "/api/v1/playlists", f"/api/v1/playlists/{made['id']}/mirrors",
        f"/api/v1/music-assistant/{server}", f"/api/v1/music-assistant/{server}/playlists", "/api/v1/servers",
        f"/api/v1/catalog/status?server={server}", "/api/v1/settings/readiness")]
    log = logs.log_file().read_text(encoding="utf-8")
    told_text = repr(told)
    for secret in _secrets(fake_ma):
        for text in [*answers, log, told_text]:
            assert secret not in text
    # Music Assistant's playlist ids stay on the server too.
    assert f"library://playlist/{key}" not in told_text and f"library://playlist/{key}" not in log


def test_a_member_and_a_guest_reach_nothing_of_another_playlist(client: TestClient, operator: object,
                                                                fake_ma: FakeMA) -> None:
    server = connect(client, fake_ma)
    made = make(client, server, "Privat", numbers(server, [fake_ma.track_by_name("Small Hours").id]))
    sync()
    with person("Mia") as other:
        assert other.get(f"/api/v1/playlists/{made['id']}").status_code == 404
        assert other.get(f"/api/v1/music-assistant/{server}").status_code == 403
        assert other.post(f"/api/v1/music-assistant/{server}/playlists/sync").status_code == 403
        assert other.delete(f"/api/v1/music-assistant/{server}").status_code == 403
        assert other.put(f"/api/v1/music-assistant/{server}/providers", json={"handles": ["0" * 16]}).status_code == 403
    # Without a sign-in nothing at all.
    from fastapi.testclient import TestClient as Plain

    with Plain(client.app, base_url="http://testserver") as nobody:
        assert nobody.get(f"/api/v1/preview/{made['items'][0]['track']}").status_code == 401
        assert nobody.get("/api/v1/music-assistant").status_code == 401
