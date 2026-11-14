"""The brake against guessing, and the tokens and keys that exist.

An app sends its token with every request, many at once. Once the token is revoked, all of
them come back 401, and if each counted as a guess, the five failures the brake allows would be gone at once: the address
would rest a quarter of an hour, and with it every other device and the sign-in of everybody behind it (a household
shares one address, and behind a proxy nexmix was not told about, everybody does). So:

* a device token or API key that exists is looked up **before** the brake is asked, and never turned away by it;
* the same wrong credential from the same sender counts only once (``Brake.first_sighting``);
* every new wrong credential counts as before: guessing is still braked.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import clock
from app.deps import require_reader
from app.main import app
from app.models import Account
from app.security import SESSION_COOKIE, brake
from app.services import apikeys

from .conftest import PASSWORD, TAB, make_account, new_client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def program() -> TestClient:
    """A program on the same address as everybody else here (the test client's)."""
    return TestClient(app, base_url="http://testserver")


@pytest.fixture(autouse=True)
def fresh_counts() -> Iterator[None]:
    brake.forget()
    apikeys.forget()
    yield
    brake.forget()
    apikeys.forget()


def _pair(account: Account, name: str) -> tuple[str, str]:
    with new_client(account) as browser:
        answer = browser.post("/api/v1/devices", json={"name": name})
        assert answer.status_code == 201, answer.text
        return str(answer.json()["token"]), str(answer.json()["device"]["id"])


def _revoke(account: Account, uid: str) -> None:
    with new_client(account) as browser:
        assert browser.delete(f"/api/v1/devices/{uid}").status_code == 204


def test_a_revoked_token_that_keeps_coming_shuts_nobody_out(operator: Account) -> None:
    """A revoked iPad keeps asking; afterwards the valid devices of others and of the same
    person still work, and so does the sign-in of a third person from the same address."""
    ada, ben, cara = make_account("ada"), make_account("ben"), make_account("cara")
    ada_token, _ = _pair(ada, "Ada's iPhone")
    ben_phone, _ = _pair(ben, "Ben's iPhone")
    ben_pad, pad_uid = _pair(ben, "Ben's iPad")
    _revoke(ben, pad_uid)
    with program() as box:
        # More than the five the brake allows, and more than the thirty of the door before the sign-in.
        for number in range(40):
            stale = box.get("/api/v1/auth/me" if number % 2 else "/api/v1/auth/sessions", headers=bearer(ben_pad))
            assert (stale.status_code, stale.json()["detail"]["code"]) == (401, "token_invalid"), number
        assert box.get("/api/v1/auth/me", headers=bearer(ada_token)).status_code == 200
        assert box.get("/api/v1/auth/me", headers=bearer(ben_phone)).status_code == 200
    with TestClient(app, base_url="http://testserver", headers=TAB) as browser:
        signed = browser.post("/api/v1/auth/login", json={"name": cara.name, "password": PASSWORD})
        assert signed.status_code == 200, signed.text


def test_a_token_nobody_holds_that_keeps_coming_does_not_close_the_door_for_others(operator: Account) -> None:
    """A device whose account was deleted takes its devices along: its token is one the database does not know at all,
    so the door before the sign-in would answer every credential it does not know with 429 after thirty wrong ones a
    minute. The same one again counts once there too; the browser of another person whose session ran out still hears
    401 and is shown the sign-in page."""
    gone = "nmd_" + "n" * 43
    with program() as box:
        for number in range(40):
            answer = box.get("/api/v1/auth/me", headers=bearer(gone))
            assert (answer.status_code, answer.json()["detail"]["code"]) == (401, "token_invalid"), number
    with TestClient(app, base_url="http://testserver", headers=TAB,
                    cookies={SESSION_COOKIE: "ran-out-long-ago"}) as browser:
        stale = browser.get("/api/v1/auth/me")
        assert (stale.status_code, stale.json()["detail"]["code"]) == (401, "sign_in_required")


def test_many_different_wrong_tokens_are_still_braked(operator: Account) -> None:
    ada = make_account("ada")
    token, _ = _pair(ada, "Ada's iPhone")
    with program() as box:
        for number in range(5):
            wrong = box.get("/api/v1/auth/me", headers=bearer("nmd_" + "g" * 40 + f"{number:03d}"))
            assert (wrong.status_code, wrong.json()["detail"]["code"]) == (401, "token_invalid")
        rest = box.get("/api/v1/auth/me", headers=bearer("nmd_" + "g" * 43))
        assert (rest.status_code, rest.json()["detail"]["code"]) == (429, "too_many_attempts")
        assert int(rest.headers["retry-after"]) > 0
        # A token that does not exist rests even when it was brought before ...
        again = box.get("/api/v1/auth/me", headers=bearer("nmd_" + "g" * 40 + "000"))
        assert again.status_code == 429
        # ... and one that exists is looked up first and goes through.
        assert box.get("/api/v1/auth/me", headers=bearer(token)).status_code == 200


def test_the_same_wrong_token_counts_again_once_its_count_is_forgotten(operator: Account,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Once only while it keeps coming within the hour a count lives; after a longer pause it is a failure again (a
    guess that waits an hour gains nothing). ``wait_seconds(key, 2)`` is 0 while the count is below two."""
    now = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    key = "addr:testclient"
    token = "nmd_" + "h" * 43
    with program() as box:
        assert box.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401
        for _round in range(20):
            for _again in range(3):
                assert box.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401
            assert brake.wait_seconds(key, 2) == 0, "the same token counted twice"
            now[0] += brake.FORGET_AFTER - 1
        now[0] += 2
        assert box.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401
        assert brake.wait_seconds(key, 1) > 0, "after a pause longer than a count lives it is a failure again"


def test_the_same_wrong_token_counts_once_per_sender_and_never_for_another_token(operator: Account) -> None:
    with program() as box:
        for _again in range(10):
            box.get("/api/v1/auth/me", headers=bearer("nmd_" + "i" * 43))
        # One failure so far: four more different ones pass, the fifth different one rests.
        for number in range(4):
            assert box.get("/api/v1/auth/me", headers=bearer("nmd_" + "j" * 40 + f"{number:03d}")).status_code == 401
        assert box.get("/api/v1/auth/me", headers=bearer("nmd_" + "k" * 43)).status_code == 429


def _switch_on_keys(operator_browser: TestClient) -> None:
    assert operator_browser.put("/api/v1/settings", json={"api_keys_allowed": True}).status_code == 200


def _key(browser: TestClient, name: str) -> tuple[str, int]:
    answer = browser.post("/api/v1/api-keys", json={"name": name})
    assert answer.status_code == 201, answer.text
    secret = str(answer.json()["secret"])
    listed = browser.get("/api/v1/api-keys").json()["keys"]
    return secret, int(next(item["id"] for item in listed if item["prefix"] == secret[:8]))


def stand_in_reader() -> TestClient:
    """An app with one route that reads (``deps.require_reader``, the way an API key is looked up for a route of the
    app: ``deps._key_account``; the app has no such route yet). It shares the brake and the database with the real one."""
    standing_in = FastAPI()

    @standing_in.get("/probe")
    def probe(_who: Annotated[Account, Depends(require_reader)]) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(standing_in)


@pytest.mark.parametrize("place", ["caller", "reader"])
def test_a_deleted_api_key_that_keeps_coming_shuts_no_other_key_out(client: TestClient, operator: Account,
                                                                      place: str) -> None:
    """The same for API keys, at both places a key is looked up: ``v1.caller`` (``/api/v1/me``) and ``deps._key_account``
    (a route that reads)."""
    _switch_on_keys(client)
    kept, _ = _key(client, "nexdeck")
    gone, gone_id = _key(client, "old script")
    assert client.delete(f"/api/v1/api-keys/{gone_id}").status_code == 204
    with program() if place == "caller" else stand_in_reader() as box:
        route = "/api/v1/me" if place == "caller" else "/probe"
        for number in range(12):
            stale = box.get(route, headers=bearer(gone))
            assert (stale.status_code, stale.json()["detail"]["code"]) == (401, "key_invalid"), number
        assert box.get(route, headers=bearer(kept)).status_code == 200


@pytest.mark.parametrize("place", ["caller", "reader"])
def test_a_key_that_exists_is_looked_up_before_the_brake(client: TestClient, operator: Account, place: str) -> None:
    _switch_on_keys(client)
    kept, _ = _key(client, "nexdeck")
    with program() if place == "caller" else stand_in_reader() as box:
        route = "/api/v1/me" if place == "caller" else "/probe"
        for number in range(5):
            assert box.get(route, headers=bearer("nmk_" + "m" * 40 + f"{number:03d}")).status_code == 401
        rest = box.get(route, headers=bearer("nmk_" + "m" * 43))
        assert (rest.status_code, rest.json()["detail"]["code"]) == (429, "too_many_attempts")
        assert box.get(route, headers=bearer(kept)).status_code == 200
