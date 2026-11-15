"""Connecting Jellyfin, against the stand-in (``fakejellyfin``): Quick Connect from the code to the token, the code
running out after ten minutes, an API key by hand, the address checked, the library and the user chosen, a token
pushed out by another sign-in, a server that is starting, letting go and connecting again."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import Account, CatalogTrack, MediaServer
from app.security import decrypt_secret
from app.services import jellyfin_link, jobs

from .conftest import new_client
from .fakejellyfin import LIBRARY_ID, SERVER_ID, FakeJellyfin
from .jellyfin_world import ADDRESS, connect, quick_connect, row


def test_quick_connect_shows_a_code_and_brings_the_token_of_the_user_who_confirmed(
        client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> None:
    begun = client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS})
    assert begun.status_code == 200, begun.text
    started = begun.json()
    assert started["name"] == "Probe"
    assert len(started["code"]) == 6 and started["code"].isdigit()
    assert 590 <= started["seconds"] <= 600
    # Nothing the browser gets names the secret, the server's id or its address.
    assert set(started) == {"id", "code", "name", "seconds"}
    secret = next(iter(fake_jellyfin.quick))
    assert secret not in begun.text and SERVER_ID not in begun.text and "jellyfin.example.com" not in begun.text
    assert client.get(f"/api/v1/jellyfin/quick/{started['id']}").json()["done"] is False
    # Not confirmed yet: nothing to redeem.
    early = client.post("/api/v1/jellyfin/connect", json={"quick": started["id"]})
    assert early.status_code == 404 and early.json()["detail"]["code"] == "quick_expired"


def test_quick_connect_from_end_to_end_keeps_the_token_sealed(client: TestClient, operator: Account,
                                                              fake_jellyfin: FakeJellyfin) -> None:
    found = quick_connect(client, fake_jellyfin)
    server = found["server"]
    assert server["kind"] == "jellyfin" and server["via"] == "quick" and server["name"] == "Probe"
    assert server["library"]["title"] == "Musik", "the only music library is chosen at once"
    assert server["user"] is None, "two users: the operator chooses"
    assert {one["name"] for one in found["users"]} == {"Erika", "Jonas"}
    assert [one["title"] for one in found["libraries"]] == ["Musik"]
    # The token is the one Jellyfin gave for nexmix's own device id, sealed with a context of this server.
    stored = row()
    token = decrypt_secret(stored.token_enc, f"jellyfin:{stored.id}")
    assert fake_jellyfin.tokens[token][1].startswith("nexmix-")
    assert decrypt_secret(stored.token_enc, "plex") == ""
    assert decrypt_secret(stored.token_enc, f"jellyfin:{stored.id + 1}") == ""
    assert token not in str(found) and LIBRARY_ID not in str(found) and SERVER_ID not in str(found)
    assert client.post("/api/v1/jellyfin/connect", json={"quick": "0" * 24}).status_code == 404


def test_a_confirmed_code_is_redeemed_once_and_its_secret_forgotten(client: TestClient, operator: Account,
                                                                    fake_jellyfin: FakeJellyfin) -> None:
    started = client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS}).json()
    fake_jellyfin.confirm(started["code"])
    assert client.post("/api/v1/jellyfin/connect", json={"quick": started["id"]}).status_code == 200
    redeemed = [path for _method, path, _params in fake_jellyfin.calls if path == "/Users/AuthenticateWithQuickConnect"]
    # Jellyfin would take the secret again for ten minutes; nexmix asks with it once and has it no more.
    again = client.post("/api/v1/jellyfin/connect", json={"quick": started["id"]})
    assert again.status_code == 404 and again.json()["detail"]["code"] == "quick_expired"
    assert client.get(f"/api/v1/jellyfin/quick/{started['id']}").status_code == 404
    assert len(redeemed) == 1
    assert [path for _method, path, _params in fake_jellyfin.calls if path == "/Users/AuthenticateWithQuickConnect"] == redeemed
    assert jellyfin_link._pending == {}


def test_the_code_runs_out_after_ten_minutes(client: TestClient, operator: Account,
                                             fake_jellyfin: FakeJellyfin, monkeypatch: pytest.MonkeyPatch) -> None:
    moment = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    started = client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS}).json()
    fake_jellyfin.tick(601)
    moment[0] += 601
    late = client.get(f"/api/v1/jellyfin/quick/{started['id']}")
    assert late.status_code == 404 and late.json()["detail"]["code"] == "quick_expired"
    assert not fake_jellyfin.confirm(started["code"])
    assert client.post("/api/v1/jellyfin/connect", json={"quick": started["id"]}).status_code == 404


def test_jellyfin_forgetting_the_code_ends_it_here_too(client: TestClient, operator: Account,
                                                      fake_jellyfin: FakeJellyfin) -> None:
    started = client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS}).json()
    fake_jellyfin.tick(600)
    assert client.get(f"/api/v1/jellyfin/quick/{started['id']}").status_code == 404
    fake_jellyfin.now -= timedelta(seconds=600)
    assert client.get(f"/api/v1/jellyfin/quick/{started['id']}").status_code == 404, "forgotten for good"


def test_a_quick_connect_belongs_to_the_session_that_began_it(client: TestClient, operator: Account,
                                                              fake_jellyfin: FakeJellyfin) -> None:
    started = client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS}).json()
    fake_jellyfin.confirm(started["code"])
    with new_client(operator) as other:
        assert other.get(f"/api/v1/jellyfin/quick/{started['id']}").status_code == 404
        assert other.post("/api/v1/jellyfin/connect", json={"quick": started["id"]}).status_code == 404
        assert other.delete(f"/api/v1/jellyfin/quick/{started['id']}").status_code == 204
    assert client.get(f"/api/v1/jellyfin/quick/{started['id']}").json()["done"] is True
    assert client.delete(f"/api/v1/jellyfin/quick/{started['id']}").status_code == 204
    assert client.post("/api/v1/jellyfin/connect", json={"quick": started["id"]}).status_code == 404


def test_quick_connect_switched_off_says_so(client: TestClient, operator: Account,
                                            fake_jellyfin: FakeJellyfin) -> None:
    fake_jellyfin.quick_enabled = False
    answer = client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS})
    assert answer.status_code == 409 and answer.json()["detail"]["code"] == "quick_off"


def test_an_api_key_by_hand_connects_and_has_no_user_of_its_own(client: TestClient, operator: Account,
                                                                fake_jellyfin: FakeJellyfin) -> None:
    answer = client.post("/api/v1/jellyfin/connect", json={"url": ADDRESS, "key": fake_jellyfin.api_key})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert found["server"]["via"] == "key"
    assert {one["name"] for one in found["users"]} == {"Erika", "Jonas"}
    assert fake_jellyfin.api_key not in answer.text
    wrong = client.post("/api/v1/jellyfin/connect", json={"url": ADDRESS, "key": "0" * 32})
    assert wrong.status_code == 502 and wrong.json()["detail"]["code"] == "jellyfin_refused"
    spaced = client.post("/api/v1/jellyfin/connect", json={"url": ADDRESS, "key": "a b"})
    assert spaced.status_code == 422 and spaced.json()["detail"]["code"] == "bad_token"


def test_old_ways_of_signing_in_are_never_used(client: TestClient, operator: Account,
                                               fake_jellyfin: FakeJellyfin) -> None:
    connect(client, fake_jellyfin)
    for _method, path, params in fake_jellyfin.calls:
        assert "api_key" not in params and "ApiKey" not in params, path


@pytest.mark.parametrize("url", ["ftp://jellyfin.example.com", "http://user:pw@jellyfin.example.com",
                                 "http://jellyfin.example.com/web", "http://metadata.example.com",
                                 "http://169.254.169.254"])
def test_an_address_that_is_no_server_s_is_refused(client: TestClient, operator: Account,
                                                   fake_jellyfin: FakeJellyfin, url: str) -> None:
    answer = client.post("/api/v1/jellyfin/quick", json={"url": url})
    assert answer.status_code == 422, answer.text
    assert answer.json()["detail"]["code"] in ("bad_url", "address_refused")
    assert fake_jellyfin.calls == []


def test_a_server_that_is_no_jellyfin_is_refused(client: TestClient, operator: Account,
                                                 fake_jellyfin: FakeJellyfin, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake_jellyfin, "name", "x")
    original = fake_jellyfin._handle

    def not_jellyfin(request):  # type: ignore[no-untyped-def]
        if request.url.path == "/System/Info/Public":
            import httpx

            return httpx.Response(200, json={"ProductName": "Something else", "Id": "nope"})
        return original(request)

    monkeypatch.setattr(fake_jellyfin, "_handle", not_jellyfin)
    answer = client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS})
    assert answer.status_code == 422 and answer.json()["detail"]["code"] == "not_jellyfin"


def test_a_starting_server_says_so_at_once_when_the_operator_waits(client: TestClient, operator: Account,
                                                                   fake_jellyfin: FakeJellyfin) -> None:
    fake_jellyfin.starting = 3
    answer = client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS})
    assert answer.status_code == 503 and answer.json()["detail"]["code"] == "jellyfin_starting"
    assert fake_jellyfin.waits == []  # type: ignore[attr-defined]


def test_library_and_user_are_chosen_by_handles_that_name_nothing_of_jellyfin(
        client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> None:
    found = quick_connect(client, fake_jellyfin)
    server = found["server"]["id"]
    choices = client.get(f"/api/v1/jellyfin/{server}/choices").json()
    assert choices["libraries"] == found["libraries"]
    assert all(len(one["handle"]) == 16 for one in choices["users"] + choices["libraries"])
    jonas = next(one for one in choices["users"] if one["name"] == "Jonas")
    assert jonas["admin"] is False
    assert client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": "f" * 16}).status_code == 404
    assert client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": "xyz"}).status_code == 422
    chosen = client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": jonas["handle"]})
    assert chosen.status_code == 200 and chosen.json()["user"]["name"] == "Jonas"
    # The library and the user chosen: the catalogue is fetched.
    assert jobs.run_pending(lane="catalog") == 1
    assert client.get(f"/api/v1/catalog/status?server={server}").json()["ready"] is True


def test_a_user_who_is_no_administrator_owns_the_playlists_himself(client: TestClient, operator: Account,
                                                                    fake_jellyfin: FakeJellyfin) -> None:
    found = quick_connect(client, fake_jellyfin, user="Jonas")
    assert [one["name"] for one in found["users"]] == ["Jonas"]
    assert found["server"]["user"]["name"] == "Jonas", "the only one there is, chosen at once"
    assert found["server"]["library"]["title"] == "Musik"
    assert jobs.run_pending(lane="catalog") == 1


def test_a_token_pushed_out_by_a_new_sign_in_of_the_same_device_is_said_as_refused(
        client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> None:
    server = connect(client, fake_jellyfin)
    stored = row()
    token = decrypt_secret(stored.token_enc, f"jellyfin:{stored.id}")
    device = fake_jellyfin.tokens[token][1]
    # Somebody signs in elsewhere with nexmix's device id: Jellyfin keeps one token per device.
    fake_jellyfin._grant(fake_jellyfin.users[0].id, device)
    assert token not in fake_jellyfin.tokens
    answer = client.get(f"/api/v1/jellyfin/{server}/choices")
    assert answer.status_code == 502 and answer.json()["detail"]["code"] == "jellyfin_refused"
    assert client.post(f"/api/v1/catalog/sync?server={server}").status_code == 202
    jobs.run_pending(lane="catalog")
    status = client.get(f"/api/v1/catalog/status?server={server}").json()
    assert status["last"]["state"] == "failed" and status["last"]["error"] == "jellyfin_refused"
    assert status["ready"] is True, "the catalogue before stays"


def test_the_device_id_is_one_per_installation_and_server(client: TestClient, operator: Account,
                                                          fake_jellyfin: FakeJellyfin) -> None:
    with SessionLocal() as db:
        first = jellyfin_link.device_id(db, SERVER_ID)
        assert jellyfin_link.device_id(db, SERVER_ID) == first
        assert jellyfin_link.device_id(db, "0" * 32) != first
    connect(client, fake_jellyfin)
    assert {device for _user, device in fake_jellyfin.tokens.values() if device} == {first}


def test_letting_go_and_coming_back_keeps_the_numbers(client: TestClient, operator: Account,
                                                      fake_jellyfin: FakeJellyfin, caplog: pytest.LogCaptureFixture
                                                      ) -> None:
    server = connect(client, fake_jellyfin)
    with SessionLocal() as db:
        before = sorted(db.scalars(select(CatalogTrack.id).where(CatalogTrack.server_id == server)))
    assert client.delete(f"/api/v1/jellyfin/{server}").status_code == 204
    with SessionLocal() as db:
        resting = db.get(MediaServer, server)
        assert resting is not None and not resting.active and resting.token_enc == "" and resting.url == ""
        assert db.scalar(select(CatalogTrack.id).where(CatalogTrack.server_id == server)) is None
    assert client.get(f"/api/v1/jellyfin/{server}").status_code == 404
    caplog.set_level(logging.DEBUG, logger="nexmix")
    again = connect(client, fake_jellyfin)
    assert again == server
    with SessionLocal() as db:
        after = sorted(db.scalars(select(CatalogTrack.id).where(CatalogTrack.server_id == server)))
    assert after == before
    text = "\n".join(record.getMessage() for record in caplog.records)
    for value in fake_jellyfin.tokens:
        assert value not in text
    assert "jellyfin.example.com" not in text


def test_a_member_cannot_connect_jellyfin(client: TestClient, fake_jellyfin: FakeJellyfin) -> None:
    from .conftest import person

    with person("mia") as member:
        assert member.post("/api/v1/jellyfin/quick", json={"url": ADDRESS}).status_code == 403
        assert member.post("/api/v1/jellyfin/connect", json={"url": ADDRESS, "key": "x"}).status_code == 403
    assert fake_jellyfin.calls == []


def test_the_secret_of_quick_connect_never_reaches_the_log(client: TestClient, operator: Account,
                                                          fake_jellyfin: FakeJellyfin,
                                                          caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    connect(client, fake_jellyfin)
    text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in fake_jellyfin.quick:
        assert secret not in text
    for token in fake_jellyfin.tokens:
        assert token not in text


def test_a_code_left_alone_is_forgotten_after_ten_minutes(client: TestClient, operator: Account,
                                                          fake_jellyfin: FakeJellyfin,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    moment = [50.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    client.post("/api/v1/jellyfin/quick", json={"url": ADDRESS})
    assert len(jellyfin_link._pending) == 1
    moment[0] += jellyfin_link.QUICK_SECONDS + 1
    jellyfin_link._sweep()
    assert jellyfin_link._pending == {}


# --- Ready for the internet? ------------------------------------------------------------------------------------------


def _jellyfin_point(client: TestClient) -> dict[str, object] | None:
    points = client.get("/api/v1/settings/readiness").json()["points"]
    return next((point for point in points if point["key"] == "jellyfin"), None)


def test_ready_for_the_internet_looks_at_jellyfin_only_when_there_is_one(client: TestClient, operator: Account,
                                                                        fake_jellyfin: FakeJellyfin) -> None:
    assert _jellyfin_point(client) is None
    # Reached at a public address: a warning (Jellyfin serves sound and pictures to whoever knows an id).
    connect(client, fake_jellyfin)
    point = _jellyfin_point(client)
    assert point is not None and point["state"] == "warn" and point["values"] == {"names": "Probe", "token": True}


def test_jellyfin_inside_the_own_network_with_its_token_is_ready(client: TestClient, operator: Account,
                                                                  fake_jellyfin: FakeJellyfin) -> None:
    quick_connect(client, fake_jellyfin, url="http://127.0.0.1:8096")
    point = _jellyfin_point(client)
    assert point is not None and point["state"] == "ok"


def test_jellyfin_without_a_readable_token_is_bad(client: TestClient, operator: Account,
                                                  fake_jellyfin: FakeJellyfin) -> None:
    quick_connect(client, fake_jellyfin, url="http://127.0.0.1:8096")
    with SessionLocal() as db:
        stored = db.scalars(select(MediaServer)).one()
        stored.token_enc = "00" * 40
        db.commit()
    point = _jellyfin_point(client)
    assert point is not None and point["state"] == "bad" and point["values"]["token"] is False
