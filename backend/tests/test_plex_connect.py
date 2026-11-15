"""Connecting Plex: signing in through plex.tv with a PIN, the servers with music and the address that answers, the
address and token by hand, the library, letting go; only the operator, and the token never leaves the server."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.db import SessionLocal
from app.models import Job, MediaServer
from app.services import plex_link
from app.services.plex import PlexError, PlexServer, check_address

from .conftest import make_account, new_client
from .fakeplex import MACHINE, FakePlex


def begin(client: TestClient) -> str:
    answer = client.post("/api/v1/plex/pin")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["url"].startswith("https://app.plex.tv/auth#?")
    assert "code=" in body["url"] and "clientID=nexmix-" in body["url"]
    return str(body["id"])


def test_the_sign_in_waits_for_plex_then_offers_only_servers_with_music(client: TestClient, operator: object,
                                                                         fake_plex: FakePlex) -> None:
    pin = begin(client)
    waiting = client.get(f"/api/v1/plex/pin/{pin}").json()
    assert waiting["done"] is False and 0 < waiting["left"] <= 600
    fake_plex.approve()
    done = client.get(f"/api/v1/plex/pin/{pin}").json()
    assert done["done"] is True
    # "Den" is shared but has no music; the player is no server at all.
    assert done["offered"] == 2
    assert [server["name"] for server in done["servers"]] == ["Studio"]
    assert done["servers"][0]["owned"] is True
    assert done["servers"][0]["libraries"] == [{"key": "3", "title": "Music"}]
    assert fake_plex.account_token not in str(done)


def test_the_address_tried_first_is_local_and_secure_and_a_relay_only_last(client: TestClient, operator: object,
                                                                         fake_plex: FakePlex) -> None:
    pin = begin(client)
    fake_plex.approve()
    client.get(f"/api/v1/plex/pin/{pin}")
    hosts = [host for method, host, path, _ in fake_plex.calls if path == "/" and host != "den.example.com"]
    assert hosts[0] == "plex-local.example.com"
    first_root = next(call for call in fake_plex.calls if call[2] == "/" and call[1] == "plex-local.example.com")
    assert first_root  # the secure local one answered, nothing else was tried for "Studio"
    assert "relay.example.com" not in hosts and "plex-remote.example.com" not in hosts
    answer = client.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0})
    assert answer.status_code == 200, answer.text
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None
        assert row.url == "https://plex-local.example.com:32400"
        assert row.via == "plextv" and row.machine_id == MACHINE


def test_when_local_is_unreachable_the_remote_address_wins_and_the_relay_stays_unused(
        client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    fake_plex.down.add("plex-local.example.com")
    pin = begin(client)
    fake_plex.approve()
    client.get(f"/api/v1/plex/pin/{pin}")
    client.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0})
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None and row.url == "https://plex-remote.example.com:32400"


def test_the_relay_is_taken_when_nothing_else_answers(client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    fake_plex.down.update({"plex-local.example.com", "plex-remote.example.com"})
    pin = begin(client)
    fake_plex.approve()
    client.get(f"/api/v1/plex/pin/{pin}")
    client.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0})
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None and row.url == "https://relay.example.com:8443"


def test_an_address_that_answers_as_another_server_does_not_count(client: TestClient, operator: object,
                                                                  fake_plex: FakePlex) -> None:
    # The local address now leads to the shared server (a reused address at home): not the server chosen.
    fake_plex.servers[0].hosts = ("plex-remote.example.com", "relay.example.com")
    fake_plex.servers[1].hosts = ("den.example.com", "plex-local.example.com")
    # The other server even takes the same token and has music: only its identity tells it apart.
    fake_plex.servers[1].token = fake_plex.account_token
    fake_plex.servers[1].music = True
    pin = begin(client)
    fake_plex.approve()
    client.get(f"/api/v1/plex/pin/{pin}")
    client.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0})
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None and row.url == "https://plex-remote.example.com:32400"


def test_connecting_with_one_library_picks_it_and_asks_for_the_catalogue_once(client: TestClient, operator: object,
                                                                             fake_plex: FakePlex) -> None:
    pin = begin(client)
    fake_plex.approve()
    client.get(f"/api/v1/plex/pin/{pin}")
    body = client.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0}).json()
    assert body["library"] == {"key": "3", "title": "Music"}
    assert body["server_name"] == "Studio"
    with SessionLocal() as db:
        assert db.query(Job).filter_by(kind="catalog_full", state="queued").count() == 1
    # The PIN is used up.
    assert client.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0}).status_code == 404


def test_a_sign_in_runs_out_after_ten_minutes(client: TestClient, operator: object, fake_plex: FakePlex,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    moment = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    pin = begin(client)
    moment[0] += 599
    assert client.get(f"/api/v1/plex/pin/{pin}").status_code == 200
    moment[0] += 2
    answer = client.get(f"/api/v1/plex/pin/{pin}")
    assert answer.status_code == 404 and answer.json()["detail"]["code"] == "pin_expired"


def test_a_sign_in_belongs_to_the_session_that_began_it(client: TestClient, operator: object,
                                                         fake_plex: FakePlex) -> None:
    from app.models import OPERATOR

    pin = begin(client)
    fake_plex.approve()
    with new_client(make_account("second", OPERATOR)) as other:
        answer = other.get(f"/api/v1/plex/pin/{pin}")
        assert answer.status_code == 404 and answer.json()["detail"]["code"] == "pin_expired"
        assert other.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0}).status_code == 404
        assert other.delete(f"/api/v1/plex/pin/{pin}").status_code == 204
    # The same operator in another browser is another session, too.
    with new_client(operator) as again:  # type: ignore[arg-type]
        assert again.get(f"/api/v1/plex/pin/{pin}").status_code == 404
    # The one who began it goes on, nothing taken away by the others.
    assert client.get(f"/api/v1/plex/pin/{pin}").json()["done"] is True
    assert client.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0}).status_code == 200
    with SessionLocal() as db:
        assert db.query(MediaServer).count() == 1


def test_a_sign_in_given_up_is_forgotten_at_once(client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    pin = begin(client)
    fake_plex.approve()
    assert client.get(f"/api/v1/plex/pin/{pin}").json()["done"] is True
    assert client.delete(f"/api/v1/plex/pin/{pin}").status_code == 204
    assert client.get(f"/api/v1/plex/pin/{pin}").status_code == 404
    assert client.post("/api/v1/plex/connect", json={"pin_id": pin, "server": 0}).status_code == 404
    # Once more, or for a PIN that never was: the same answer.
    assert client.delete(f"/api/v1/plex/pin/{pin}").status_code == 204
    assert client.delete("/api/v1/plex/pin/abc").status_code == 204
    with new_client(make_account("mila")) as member:
        assert member.delete(f"/api/v1/plex/pin/{pin}").status_code == 403


def test_plex_tv_out_of_reach_is_said_so(client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    fake_plex.down.add("plex.tv")
    answer = client.post("/api/v1/plex/pin")
    assert answer.status_code == 502 and answer.json()["detail"]["code"] == "plextv_unreachable"


def test_by_hand_with_address_and_token(client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400/", "token": fake_plex.account_token})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["via"] == "manual" and body["server_name"] == "Studio"
    assert fake_plex.account_token not in answer.text
    with SessionLocal() as db:
        row = db.query(MediaServer).one()
        assert row.url == "http://127.0.0.1:32400"
        # Sealed, not kept as it is.
        assert fake_plex.account_token not in row.token_enc
        assert plex_link.token_of(row) == fake_plex.account_token


@pytest.mark.parametrize(("url", "code"), [
    ("plex.example.com:32400", "bad_url"),
    ("ftp://plex.example.com", "bad_url"),
    ("http://user:pass@plex.example.com:32400", "bad_url"),
    ("http://plex.example.com:32400/library", "bad_url"),
    ("http://plex.example.com:32400/?x=1", "bad_url"),
    ("http://169.254.169.254", "address_refused"),
    ("http://metadata.example.com", "address_refused"),
    ("http://[fe80::1]:32400", "address_refused"),
])
def test_an_address_that_is_no_plex_address_is_refused_before_anything_is_sent(
        client: TestClient, operator: object, fake_plex: FakePlex, url: str, code: str) -> None:
    answer = client.post("/api/v1/plex/connect", json={"url": url, "token": "abc"})
    assert answer.status_code == 422
    assert answer.json()["detail"]["code"] == code
    assert fake_plex.library_calls() == []


def test_a_wrong_token_by_hand_is_refused_and_nothing_is_kept(client: TestClient, operator: object,
                                                              fake_plex: FakePlex) -> None:
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": "not-the-token"})
    assert answer.status_code == 502 and answer.json()["detail"]["code"] == "plex_refused"
    with SessionLocal() as db:
        assert db.query(MediaServer).count() == 0


def test_a_token_with_spaces_is_no_token(client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": "a b"})
    assert answer.status_code == 422 and answer.json()["detail"]["code"] == "bad_token"


def test_another_library_is_picked_from_the_server_only(client: TestClient, operator: object,
                                                        fake_plex: FakePlex) -> None:
    client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake_plex.account_token})
    assert client.get("/api/v1/plex/libraries").json() == {"libraries": [{"key": "3", "title": "Music"}]}
    # The films are no music library, and a made-up key is no library at all.
    assert client.put("/api/v1/plex/library", json={"key": "1"}).status_code == 404
    assert client.put("/api/v1/plex/library", json={"key": "77"}).status_code == 404
    assert client.put("/api/v1/plex/library", json={"key": "../3"}).status_code == 422
    assert client.put("/api/v1/plex/library", json={"key": "3"}).status_code == 200


def test_disconnecting_forgets_token_and_catalogue(client: TestClient, operator: object, fake_plex: FakePlex) -> None:
    from app.services import jobs

    client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake_plex.account_token})
    jobs.run_pending(lane="catalog")
    assert client.get("/api/v1/catalog/status").json()["counts"]["tracks"] > 0
    assert client.delete("/api/v1/plex").status_code == 204
    status = client.get("/api/v1/catalog/status").json()
    assert status["connected"] is False and status["counts"]["tracks"] == 0
    assert client.get("/api/v1/plex").json() == {"connected": False}
    with SessionLocal() as db:
        # The server rests: its row stays (the numbers of its things, the copies of the playlists), its token and its
        # address do not.
        rows = db.query(MediaServer).all()
        assert [(row.active, row.token_enc, row.url, row.generation) for row in rows] == [(False, "", "", 0)]


def test_a_member_may_neither_connect_nor_look(fake_plex: FakePlex) -> None:
    member = make_account("mila")
    with new_client(member) as browser:
        for method, path, body in (
            ("GET", "/api/v1/plex", None),
            ("POST", "/api/v1/plex/pin", None),
            ("GET", "/api/v1/plex/pin/4001", None),
            ("POST", "/api/v1/plex/connect", {"url": "http://127.0.0.1:32400", "token": fake_plex.account_token}),
            ("GET", "/api/v1/plex/libraries", None),
            ("PUT", "/api/v1/plex/library", {"key": "3"}),
            ("DELETE", "/api/v1/plex", None),
            ("POST", "/api/v1/catalog/sync", None),
        ):
            answer = browser.request(method, path, json=body)
            assert answer.status_code == 403, (method, path, answer.status_code)
            assert answer.json()["detail"]["code"] == "operator_only"
    assert fake_plex.calls == []


def test_the_token_never_reaches_the_log(client: TestClient, operator: object, fake_plex: FakePlex,
                                         caplog: pytest.LogCaptureFixture) -> None:
    from app.services import jobs

    caplog.set_level(logging.DEBUG)
    client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake_plex.account_token})
    fake_plex.fail_after = fake_plex.library_requests + 5
    jobs.run_pending(lane="catalog")
    assert fake_plex.account_token not in caplog.text
    assert "X-Plex-Token" not in caplog.text


def test_the_token_goes_in_a_header_never_in_an_address(fake_plex: FakePlex) -> None:
    with PlexServer("http://127.0.0.1:32400", fake_plex.account_token, "nexmix-test") as server:
        server.identity()
        list(server.pages("3", 10))
        server.count("3", 10, {"updatedAt>>": 5})
    for _method, _host, _path, params in fake_plex.calls:
        assert fake_plex.account_token not in str(params)


def test_the_client_says_why_it_failed(fake_plex: FakePlex) -> None:
    for mode, code in (("503", "plex_busy"), ("timeout", "plex_timeout"), ("drop", "plex_unreachable"),
                       ("401", "plex_refused")):
        fake_plex.fail_after, fake_plex.fail_mode = fake_plex.library_requests, mode
        with PlexServer("http://127.0.0.1:32400", fake_plex.account_token, "nexmix-test") as server,                 pytest.raises(PlexError) as failure:
            server.music_sections()
        assert failure.value.code == code
    fake_plex.fail_after = None
    fake_plex.down.add("127.0.0.1")
    with PlexServer("http://127.0.0.1:32400", fake_plex.account_token, "nexmix-test") as server,             pytest.raises(PlexError) as failure:
        server.identity()
    assert failure.value.code == "plex_unreachable"


def test_a_redirect_is_not_followed(fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.services import plex

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    monkeypatch.setattr(plex, "TRANSPORT", httpx.MockTransport(redirect))
    with PlexServer("http://127.0.0.1:32400", "token", "nexmix-test") as server, pytest.raises(PlexError) as failure:
        server.identity()
    assert failure.value.code == "plex_error"


def test_check_address_keeps_scheme_host_and_port_only(fake_plex: FakePlex) -> None:
    assert check_address(" https://plex.example.com:32400/ ") == "https://plex.example.com:32400"
    assert check_address("http://127.0.0.1") == "http://127.0.0.1"
    with pytest.raises(PlexError):
        check_address("http://plex.example.com:notaport")
