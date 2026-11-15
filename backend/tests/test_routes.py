"""The interface is one: every route of the app is under ``/api/v1``, every route that is not open says 401 without a
sign-in, the description of the interface (``/api/v1/openapi.json``) is kept as a snapshot, and its page works under
the strict policy of the app.

When a route is added or changed on purpose, the snapshot is renewed by hand:

    NEXMIX_RECORD_OPENAPI=1 python -m pytest tests/test_routes.py

and the open routes below are named here if the new route is open to everybody (it almost never should be).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import deps
from app.main import app, create_app
from app.models import Account

SNAPSHOT = Path(__file__).parent / "schema" / "openapi.json"

#: Routes that answer without a sign-in, on purpose: the state before and while signing in, the things the sign-in page
#: shows, the way back in (a link, a code) and the provider's return. (The description of the interface and its page are
#: routes only where the operator turned them on, ``NEXMIX_API_DOCS``; they are open then, see the tests below.)
OPEN = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/about"),
    ("GET", "/api/v1/locales"),
    ("GET", "/api/v1/locales/{code}"),
    ("GET", "/api/v1/setup"),
    ("POST", "/api/v1/setup"),
    ("GET", "/api/v1/auth/methods"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/login/totp"),
    ("POST", "/api/v1/auth/login/totp/cancel"),
    ("POST", "/api/v1/auth/passkey/begin"),
    ("POST", "/api/v1/auth/passkey"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/forgot"),
    # Sign-in through a provider: the buttons, off to the provider, and back (the second return belongs to the entry
    # ``oidc``, the standard address of the shared module).
    ("GET", "/api/v1/oidc/providers"),
    ("GET", "/api/v1/oidc/{slug}/start"),
    ("GET", "/api/v1/oidc/{slug}/callback"),
    ("GET", "/api/v1/oidc/callback"),
    ("GET", "/api/v1/invite/{token}"),
    ("POST", "/api/v1/invite/{token}"),
    ("GET", "/api/v1/email/confirm/{token}"),
    ("POST", "/api/v1/email/confirm/{token}"),
    ("GET", "/api/v1/reset/{token}"),
    ("POST", "/api/v1/reset/{token}"),
    # The link of an invitation for guests: what it shows before the name, joining with a name, the mosaic's covers.
    # Each only with a running invitation's code, through the brakes of the address (``services/guests.py``).
    ("GET", "/api/v1/join/{code}"),
    ("POST", "/api/v1/join/{code}"),
    ("GET", "/api/v1/join/{code}/cover/{album_id}"),
}


def concrete(path: str) -> str:
    """A path with its parameters filled in, so that the router finds the route."""
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", lambda m: "1" if m.group(1).endswith("id") else "a" * 32, path)


def every_route() -> list[Any]:
    """The routes of the app, the included routers opened (this FastAPI keeps them as a router inside a router)."""
    found: list[Any] = []
    for route in app.routes:
        inner = getattr(route, "original_router", None)
        found.extend(inner.routes if inner is not None else [route])
    return found


def api_routes() -> list[tuple[str, str]]:
    found = []
    for route in every_route():
        if isinstance(route, APIRoute):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                found.append((method, route.path))
    return sorted(found)


def test_every_route_is_under_the_one_interface() -> None:
    outside = [route.path for route in every_route() if not str(getattr(route, "path", "")).startswith("/api/v1")]
    # The frontend is served from files and mounted only where it was built (not in the tests).
    assert outside == [], f"Routes outside /api/v1: {outside}"
    assert len(api_routes()) > 80


def test_outside_the_interface_there_are_only_the_files_of_the_built_app(tmp_path: Path) -> None:
    """Where the interface is built and mounted, the routes outside ``/api/v1`` are counted: the folder of the scripts
    and styles, the manifest, and the one route that serves a file of the app (or its first page). Nothing else, and no
    route of this list ever changes anything (a ``POST`` outside the interface would be a way in around it)."""
    from fastapi import FastAPI

    from app import main

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>nexmix</title>", encoding="utf-8")
    built = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)
    main._mount_frontend(built, dist)
    routes = [(sorted(getattr(route, "methods", None) or ["MOUNT"]), route.path) for route in built.routes]
    assert routes == [
        (["MOUNT"], "/assets"),
        (["GET"], "/manifest.webmanifest"),
        (["GET"], "/{path:path}"),
    ]
    with TestClient(built) as browser:
        assert browser.post("/anything", json={}).status_code == 405
        assert browser.get("/api/v1/nothing-here").status_code == 404


def test_the_app_has_one_health_route_and_no_second_one_outside() -> None:
    assert [path for method, path in api_routes() if path.endswith("health")] == ["/api/v1/health"]



def test_nothing_is_called_under_the_old_prefix() -> None:
    with TestClient(app, base_url="http://testserver") as client:
        for path in ("/api/auth/me", "/api/health", "/api/setup", "/api/about"):
            assert client.get(path).status_code == 404, path


def test_every_route_that_is_not_open_says_401_without_a_sign_in() -> None:
    wrong: list[str] = []
    open_found: set[tuple[str, str]] = set()
    with TestClient(app, base_url="http://testserver", headers={"X-Nexmix-Client": "tab-routes000"}) as client:
        for method, path in api_routes():
            if (method, path) in OPEN:
                open_found.add((method, path))
                continue
            answer = client.request(method, concrete(path), json={} if method in ("POST", "PUT", "PATCH") else None)
            if answer.status_code != 401:
                wrong.append(f"{method} {path} -> {answer.status_code}")
    assert wrong == [], "Routes that do not ask for a sign-in (name them in OPEN if that is meant): " + "; ".join(wrong)
    stale = sorted(OPEN - open_found)
    assert stale == [], f"Named open here, but not routes any more: {stale}"


def test_a_device_token_that_is_wrong_is_401_on_every_route_that_asks_for_one() -> None:
    """The same list with a token that does not exist: still 401 (or 403 where only a browser may come)."""
    odd: list[str] = []
    with TestClient(app, base_url="http://testserver") as client:
        for method, path in api_routes():
            if (method, path) in OPEN or path == "/api/v1/me":
                continue
            answer = client.request(method, concrete(path), headers={"Authorization": "Bearer nmd_" + "w" * 43},
                                    json={} if method in ("POST", "PUT", "PATCH") else None)
            if answer.status_code not in (401, 403, 429):
                odd.append(f"{method} {path} -> {answer.status_code}")
    assert odd == [], odd


def _called(dependant: Any) -> set[Any]:
    """Every function a route asks for before it runs, the ones asked for by the ones asked for included."""
    found: set[Any] = set()
    for sub in dependant.dependencies:
        found.add(sub.call)
        found |= _called(sub)
    return found


def routes_asking_for(*guards: Any) -> list[tuple[str, str]]:
    found = []
    for route in every_route():
        if isinstance(route, APIRoute) and _called(route.dependant) & set(guards):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                found.append((method, route.path))
    return sorted(found)


#: The routes a device token reaches (the sign-in state and the own profile, the push registration of the device):
#: everything else needs the browser. A new route lands here only on purpose.
TOKEN_ROUTES = {
    ("GET", "/api/v1/about/updates"),
    ("POST", "/api/v1/auth/setup/done"),
    ("POST", "/api/v1/auth/setup/codes"),
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/auth/sessions"),
    ("PUT", "/api/v1/me/profile"),
    ("POST", "/api/v1/me/whats-new/seen"),
    ("PUT", "/api/v1/me/language"),
    ("PUT", "/api/v1/me/preferences"),
    ("PUT", "/api/v1/auth/avatar"),
    ("DELETE", "/api/v1/auth/avatar"),
    ("GET", "/api/v1/avatars/{account_id}"),
    ("GET", "/api/v1/push"),
    ("POST", "/api/v1/push/devices"),
    ("POST", "/api/v1/push/devices/lookup"),
    ("PUT", "/api/v1/push/devices/{device_id}"),
    ("DELETE", "/api/v1/push/devices/{device_id}"),
    ("POST", "/api/v1/push/test"),
    # Playlists: making and changing them, their entries and shares (a key only reads, see below).
    ("POST", "/api/v1/playlists"),
    ("PUT", "/api/v1/playlists/{playlist_id}"),
    ("DELETE", "/api/v1/playlists/{playlist_id}"),
    ("POST", "/api/v1/playlists/{playlist_id}/restore"),
    ("POST", "/api/v1/playlists/{playlist_id}/items"),
    ("POST", "/api/v1/playlists/{playlist_id}/items/remove"),
    ("POST", "/api/v1/playlists/{playlist_id}/items/restore"),
    ("POST", "/api/v1/playlists/{playlist_id}/items/move"),
    ("GET", "/api/v1/playlists/{playlist_id}/shares"),
    ("PUT", "/api/v1/playlists/{playlist_id}/shares/{account_id}"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/shares/{account_id}"),
    # Suggestions: "not again" and its undo.
    ("POST", "/api/v1/suggestions/hidden"),
    ("DELETE", "/api/v1/suggestions/hidden/{track_id}"),
    # The invitation of a playlist for guests (a key reaches none of it).
    ("GET", "/api/v1/playlists/{playlist_id}/invite"),
    ("POST", "/api/v1/playlists/{playlist_id}/invite"),
    ("PUT", "/api/v1/playlists/{playlist_id}/invite"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/invite"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/invite/guests/{guest_id}"),
    # The sync with Plex: now, and the note of an order sorted on both sides (take Plex's, or keep this one).
    ("POST", "/api/v1/playlists/{playlist_id}/sync"),
    ("POST", "/api/v1/playlists/{playlist_id}/plex-order"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/plex-order"),
    ("POST", "/api/v1/playlists/{playlist_id}/missing/remove"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/missing"),
    # Hiding a playlist from the own overview, and showing it again.
    ("PUT", "/api/v1/playlists/{playlist_id}/hidden"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/hidden"),
    # Mirrors on other servers: reading them, the preview, copying and moving, choices by hand, tracks only there.
    ("GET", "/api/v1/playlists/{playlist_id}/mirrors"),
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors/preview"),
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors"),
    ("POST", "/api/v1/playlists/{playlist_id}/move"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}"),
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/remake"),
    ("PUT", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/entries/{entry_id}"),
    ("POST", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/extras/{extra_id}/take"),
    ("DELETE", "/api/v1/playlists/{playlist_id}/mirrors/{server_id}/extras/{extra_id}"),
}

#: The routes that only read and that a program with an API key reaches too (as the browser and a device do). A route
#: that changes something is never in this list. Routes that read for a program are added here.
READ_ROUTES: set[tuple[str, str]] = {
    # The media servers: names and how they stand, never where they are.
    ("GET", "/api/v1/servers"),
    # Where the catalogue stands, and the covers of its albums.
    ("GET", "/api/v1/catalog/status"),
    ("GET", "/api/v1/catalog/cover/{album_id}"),
    # The compact catalogue, the search on the server, the filters, an album, an artist.
    ("GET", "/api/v1/catalog"),
    ("GET", "/api/v1/catalog/search"),
    ("GET", "/api/v1/catalog/facets"),
    ("GET", "/api/v1/catalog/albums/{album_id}"),
    ("GET", "/api/v1/catalog/artists/{artist_id}"),
    # The playlists the key's account sees, one with its entries, and its live events.
    ("GET", "/api/v1/playlists"),
    ("GET", "/api/v1/playlists/{playlist_id}"),
    ("GET", "/api/v1/playlists/{playlist_id}/events"),
    # A playlist's mirrors on other servers.
    ("GET", "/api/v1/playlists/{playlist_id}/mirrors"),
    # Suggestions, filling by mood, the two voices, and what the account asked not to see again.
    ("GET", "/api/v1/suggestions"),
    ("GET", "/api/v1/suggestions/mood"),
    ("GET", "/api/v1/suggestions/together"),
    ("GET", "/api/v1/suggestions/sonic"),
    ("GET", "/api/v1/suggestions/hidden"),
}


def test_the_routes_a_device_token_reaches_are_the_ones_named_here() -> None:
    reached = set(routes_asking_for(deps.require_account, deps.require_reader))
    assert reached == TOKEN_ROUTES | READ_ROUTES, (
        f"new: {sorted(reached - TOKEN_ROUTES - READ_ROUTES)}; gone: {sorted(TOKEN_ROUTES | READ_ROUTES - reached)}")


def test_the_routes_a_key_reaches_only_read() -> None:
    reading = set(routes_asking_for(deps.require_reader))
    assert reading == READ_ROUTES, f"new: {sorted(reading - READ_ROUTES)}; gone: {sorted(READ_ROUTES - reading)}"
    assert {method for method, _path in reading} <= {"GET"}


def test_no_operator_route_is_reached_with_a_device_token(client: TestClient, operator: Account) -> None:
    """The operator's routes are found in the route list, not named by hand, so a route added tomorrow is covered."""
    operator_routes = routes_asking_for(deps.require_operator)
    assert ("GET", "/api/v1/settings") in operator_routes and ("POST", "/api/v1/invites") in operator_routes
    assert len(operator_routes) > 20, "the guard is found on the routes (a floor: this list may grow, not shrink)"
    token = client.post("/api/v1/devices", json={"name": "phone"}).json()["token"]
    odd: list[str] = []
    with TestClient(app, base_url="http://testserver") as device:
        for method, path in operator_routes:
            answer = device.request(method, concrete(path), headers={"Authorization": f"Bearer {token}"},
                                    json={} if method in ("POST", "PUT", "PATCH") else None)
            code = answer.json().get("detail", {}).get("code") if answer.headers.get("content-type", "").startswith(
                "application/json") else None
            if (answer.status_code, code) != (403, "browser_only"):
                odd.append(f"{method} {path} -> {answer.status_code} {code}")
    assert odd == [], odd
    # The same account in its browser still gets in.
    assert client.get("/api/v1/settings").status_code == 200


def test_the_description_of_the_interface_is_the_snapshot() -> None:
    current = json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("NEXMIX_RECORD_OPENAPI") == "1":
        SNAPSHOT.write_text(current, encoding="utf-8", newline="\n")
    assert SNAPSHOT.is_file(), "No snapshot of the description; record it (see the top of this file)."
    assert SNAPSHOT.read_text(encoding="utf-8") == current, (
        "The interface changed. If that is on purpose, renew the snapshot: "
        "NEXMIX_RECORD_OPENAPI=1 python -m pytest tests/test_routes.py (and commit the file)."
    )


def test_every_route_of_the_interface_is_in_its_description() -> None:
    """No route is left out of the description (the download of the log once was): what the interface offers, a
    program can read there."""
    described = {(method.upper(), path) for path, operations in app.openapi()["paths"].items() for method in operations}
    assert [route for route in api_routes() if route not in described] == []
    assert ("GET", "/api/v1/logs/download") in described


def test_the_description_and_its_page_are_off_unless_the_operator_turns_them_on() -> None:
    """An instance reached from the internet does not hand out the list of its routes unasked (``NEXMIX_API_DOCS``)."""
    for path in ("/api/v1/openapi.json", "/api/v1/docs", "/api/docs", "/api/openapi.json", "/docs", "/redoc"):
        with TestClient(app, base_url="http://testserver") as client:
            answer = client.get(path)
        assert answer.status_code == 404, path
    assert not [path for method, path in api_routes() if path in ("/api/v1/docs", "/api/v1/openapi.json")]
    # The setting is what decides, and it is off from the start.
    from app.config import Settings

    assert Settings.model_fields["api_docs"].default is False


def test_the_description_is_served_and_names_the_ways_in() -> None:
    with TestClient(create_app(api_docs=True), base_url="http://testserver") as client:
        answer = client.get("/api/v1/openapi.json")
        assert answer.status_code == 200 and answer.json()["info"]["title"] == "nexmix"
        assert all(path.startswith("/api/v1/") for path in answer.json()["paths"])
        assert "HTTPBearer" in str(answer.json()["components"]["securitySchemes"])


def test_the_settings_say_whether_the_description_is_served(client: TestClient, operator: Account) -> None:
    """The settings page links to the description only where the server serves it; elsewhere it says how to turn it
    on. The server tells which, from what the app was built with."""
    from .conftest import TAB, sign_in

    assert client.get("/api/v1/settings").json()["api_docs"] is False
    for documented in (False, True):
        with TestClient(create_app(api_docs=documented), base_url="http://testserver", headers=TAB) as browser:
            sign_in(browser, operator)
            answer = browser.get("/api/v1/settings")
            assert answer.status_code == 200 and answer.json()["api_docs"] is documented
            # What the card links to is there exactly then.
            for path in ("/api/v1/openapi.json", "/api/v1/docs"):
                assert browser.get(path).status_code == (200 if documented else 404), (documented, path)


def test_the_page_of_the_interface_needs_no_script_and_loads_nothing_from_elsewhere() -> None:
    with TestClient(create_app(api_docs=True), base_url="http://testserver") as client:
        page = client.get("/api/v1/docs")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        text = page.text
        assert "<script" not in text and "http://" not in text and "https://" not in text
        assert "/api/v1/auth/login" in text and "Authorization: Bearer" in text
        policy = page.headers["content-security-policy"]
        assert "default-src 'self'" in policy and "script-src" not in policy


@pytest.mark.parametrize("path", ["/api/v1/me", "/api/v1/auth/me", "/api/v1/devices"])
def test_what_the_interface_answers_is_never_kept(path: str) -> None:
    with TestClient(app, base_url="http://testserver") as client:
        assert client.get(path).headers["cache-control"] == "no-store"
