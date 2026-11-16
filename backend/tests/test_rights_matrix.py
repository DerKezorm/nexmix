"""The rights of every route for every kind of caller, found in the route list, never named route by route: a route
added tomorrow is covered as soon as it exists.

* **A member** reaches no route of the operator (403).
* **An API key** reaches the reading routes it is meant for and nothing else (401 or 403 everywhere else).
* **The routes of one playlist** (every path with ``{playlist_id}``): a member it is not shared with gets 404 at every
  one of them, a member who may only see it gets 403 or 404 at every one but reading it, and a member who may change it
  manages nothing (deleting it, its shares). Each such route needs a body below that gets past the checks of its shape,
  so that a 422 never stands in for a refusal.

Without a sign-in (401), with a wrong or a device token, with a guest cookie: ``test_routes.py`` and ``test_guests.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.models import Account
from app.routers import playlists as playlists_router

from .conftest import make_account, new_client
from .test_playlists import People, make
from .test_routes import OPEN, READ_ROUTES, api_routes, concrete, routes_asking_for


def test_a_member_reaches_no_route_of_the_operator(client: TestClient, operator: Account) -> None:
    routes = routes_asking_for(deps.require_operator)
    assert len(routes) > 20
    odd: list[str] = []
    with new_client(make_account("mia")) as member:
        for method, path in routes:
            answer = member.request(method, concrete(path), json={} if method in ("POST", "PUT", "PATCH") else None)
            if answer.status_code != 403:
                odd.append(f"{method} {path} -> {answer.status_code}")
    assert odd == []


def test_an_api_key_reaches_the_reading_routes_and_nothing_else(client: TestClient, operator: Account) -> None:
    assert client.put("/api/v1/settings", json={"api_keys_allowed": True}).status_code == 200
    secret = client.post("/api/v1/api-keys", json={"name": "script"}).json()["secret"]
    odd: list[str] = []
    with TestClient(client.app, base_url="http://testserver") as program:
        bearer = {"Authorization": f"Bearer {secret}"}
        for method, path in api_routes():
            if (method, path) in OPEN or path.startswith(("/api/v1/guest", "/api/v1/join")):
                continue
            # The event stream would stay open; its right is the reader's, checked in test_playlists.py.
            if path.endswith("/events"):
                continue
            answer = program.request(method, concrete(path), headers=bearer,
                                     json={} if method in ("POST", "PUT", "PATCH") else None)
            reading = (method, path) in READ_ROUTES or path == "/api/v1/me"
            if reading and answer.status_code in (401, 403):
                odd.append(f"{method} {path} -> {answer.status_code}, meant to be read")
            if not reading and answer.status_code not in (401, 403):
                odd.append(f"{method} {path} -> {answer.status_code}")
    assert odd == []


# --- The routes of one playlist ---------------------------------------------------------------------------------------

#: A body for every route of a playlist that changes something, good enough for its shape.
BODIES: dict[tuple[str, str], Any] = {
    ("PUT", "/api/v1/playlists/{playlist_id}"): {"name": "Neuer Name"},
    ("POST", "/api/v1/playlists/{playlist_id}/items"): {"tracks": [302]},
    ("POST", "/api/v1/playlists/{playlist_id}/items/remove"): {"entries": ["{entry}"]},
    ("POST", "/api/v1/playlists/{playlist_id}/items/restore"): {"entries": ["{entry}"], "after": None},
    ("POST", "/api/v1/playlists/{playlist_id}/items/move"): {"entry": "{entry}", "after": None},
    ("PUT", "/api/v1/playlists/{playlist_id}/shares/{account_id}"): {"access": "read"},
    ("PUT", "/api/v1/playlists/{playlist_id}/invite"): {"start": True},
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors/preview"): {"server": 2},
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors"): {"server": 2},
    ("POST", "/api/v1/playlists/{playlist_id}/move"): {"server": 2},
    ("PUT", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/entries/{entry_id}"): {"track": None},
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/extras/{extra_id}/take"): {"track": 301},
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/emptied"): {"empty": False},
}
#: What one who may only see a playlist reaches of it.
VIEW = {("GET", "/api/v1/playlists/{playlist_id}"), ("GET", "/api/v1/playlists/{playlist_id}/events"),
        ("GET", "/api/v1/playlists/{playlist_id}/mirrors"),
        # Hiding it from the own view, and showing it again: the account's own business, nothing changes for others.
        ("PUT", "/api/v1/playlists/{playlist_id}/hidden"), ("DELETE", "/api/v1/playlists/{playlist_id}/hidden")}
#: What only the owner and the operator do: delete, bring back, share.
MANAGE = {
    ("DELETE", "/api/v1/playlists/{playlist_id}"),
    ("POST", "/api/v1/playlists/{playlist_id}/restore"),
    ("GET", "/api/v1/playlists/{playlist_id}/shares"),
    ("PUT", "/api/v1/playlists/{playlist_id}/shares/{account_id}"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/shares/{account_id}"),
    # Copying and moving to another server, and the mirrors themselves.
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors/preview"),
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors"),
    ("POST", "/api/v1/playlists/{playlist_id}/move"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}"),
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/remake"),
}


def playlist_routes() -> list[tuple[str, str]]:
    return [(method, path) for method, path in api_routes() if "{playlist_id}" in path]


def _fill(value: Any, entry: int) -> Any:
    if value == "{entry}":
        return entry
    if isinstance(value, list):
        return [_fill(one, entry) for one in value]
    if isinstance(value, dict):
        return {key: _fill(one, entry) for key, one in value.items()}
    return value


def _ask(browser: TestClient, method: str, path: str, playlist: dict[str, Any], people: People) -> int:
    body = BODIES.get((method, path))
    if body is None and method in ("POST", "PUT", "PATCH"):
        body = {}
    url = (path.replace("{playlist_id}", str(playlist["id"]))
           .replace("{account_id}", str(people.stranger_row.id)).replace("{guest_id}", "1")
           .replace("{server_id}", "2").replace("{entry_id}", str(playlist["items"][0]["id"]))
           .replace("{extra_id}", "1"))
    if path.endswith("/events"):
        url += "?probe=1"
    return browser.request(method, url, json=_fill(body, playlist["items"][0]["id"])).status_code


@pytest.fixture
def people() -> Any:
    found = People()
    yield found
    found.close()


@pytest.fixture
def shared(people: People) -> dict[str, Any]:
    from . import made_up_catalog

    made_up_catalog.install()
    playlist = make(people.owner, tracks=[301, 311, 317])
    for row, access in ((people.reader_row, "read"), (people.editor_row, "edit")):
        assert people.owner.put(f"/api/v1/playlists/{playlist['id']}/shares/{row.id}",
                                json={"access": access}).status_code == 200
    return people.owner.get(f"/api/v1/playlists/{playlist['id']}").json()


def test_every_route_of_a_playlist_with_a_body_is_named_here() -> None:
    changing = {(method, path) for method, path in playlist_routes() if method in ("POST", "PUT", "PATCH")}
    # Routes that take no body at all.
    bodiless = {("POST", "/api/v1/playlists/{playlist_id}/restore"), ("POST", "/api/v1/playlists/{playlist_id}/sync"),
                ("POST", "/api/v1/playlists/{playlist_id}/invite"),
                ("POST", "/api/v1/playlists/{playlist_id}/plex-order"),
                ("POST", "/api/v1/playlists/{playlist_id}/missing/remove"),
                ("PUT", "/api/v1/playlists/{playlist_id}/hidden"),
                ("POST", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/remake")}
    assert changing - bodiless == set(BODIES)
    assert VIEW | MANAGE <= set(playlist_routes())
    assert len(playlist_routes()) >= 20


@pytest.mark.parametrize("who", ["stranger", "reader", "editor"])
def test_every_route_of_a_playlist_refuses_who_may_not(people: People, shared: dict[str, Any], who: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playlists_router, "STREAM_SECONDS", 0.05)
    browser = getattr(people, who)
    odd: list[str] = []
    for method, path in playlist_routes():
        status = _ask(browser, method, path, shared, people)
        if who == "stranger":
            wanted: tuple[int, ...] = (404,)
        elif who == "reader":
            wanted = (200,) if (method, path) in VIEW else (403, 404)
        else:
            if (method, path) not in MANAGE:
                continue
            wanted = (403,)
        if status not in wanted:
            odd.append(f"{who}: {method} {path} -> {status}, wanted {wanted}")
    assert odd == []
    if who == "editor":
        return
    # Nothing changed on the way.
    after = people.owner.get(f"/api/v1/playlists/{shared['id']}").json()
    assert [item["track"] for item in after["items"]] == [301, 311, 317] and after["name"] == shared["name"]
