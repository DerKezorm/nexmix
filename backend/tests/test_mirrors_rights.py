"""Who may do what with mirrors, copies and moves, with a real mirror on the stand-in Jellyfin: a guest reaches nothing,
a stranger sees nothing, one who may listen only reads the mirrors, one who may change chooses tracks and handles
what is only in a mirror, the owner and the operator copy, move and let go; "take all over" and letting go of a server
are the operator's. No answer names Jellyfin's ids, its address or a token."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models import OPERATOR

from .conftest import make_account, new_client
from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .mirror_world import connect_both, extras, mirror_key, plex_numbers
from .plex_world import sync
from .test_guests_and_plex import invite, phone
from .test_plex_and_jellyfin import Seen, secrets_of


@pytest.fixture
def setting(client: TestClient, fake_plex: FakePlex, fake_jellyfin: FakeJellyfin) -> Any:
    operator_row = make_account("otto", OPERATOR)
    operator = new_client(operator_row)
    _plex, jelly, _twins = connect_both(operator, fake_plex, fake_jellyfin, frozenset({3021}))
    owner_row, reader_row, editor_row = make_account("olivia"), make_account("rafael"), make_account("edith")
    owner, reader, editor, stranger = (new_client(row) for row in (owner_row, reader_row, editor_row,
                                                                  make_account("stella")))
    made = owner.post("/api/v1/playlists", json={"name": "Grillabend"}).json()
    playlist_id = made["id"]
    assert owner.post(f"/api/v1/playlists/{playlist_id}/items",
                      json={"tracks": plex_numbers([3001, 3003, 3021])}).status_code == 200
    for row, access in ((reader_row, "read"), (editor_row, "edit")):
        assert owner.put(f"/api/v1/playlists/{playlist_id}/shares/{row.id}", json={"access": access}).status_code \
            == 200
    sync(fake_plex)
    yield {"operator": operator, "owner": owner, "reader": reader, "editor": editor, "stranger": stranger,
           "playlist": playlist_id, "jelly": jelly}
    for one in (operator, owner, reader, editor, stranger):
        one.close()


def _routes(playlist: int, jelly: int, entry: int, extra: int) -> list[tuple[str, str, Any, str]]:
    base = f"/api/v1/playlists/{playlist}"
    return [
        ("GET", f"{base}/mirrors", None, "view"),
        ("POST", f"{base}/mirrors/preview", {"server": jelly}, "manage"),
        ("PUT", f"{base}/mirrors/{jelly}/entries/{entry}", {"track": None}, "edit"),
        ("DELETE", f"{base}/mirrors/{jelly}/extras/{extra}", None, "edit"),
        ("POST", f"{base}/move", {"server": jelly}, "manage"),
        ("DELETE", f"{base}/mirrors/{jelly}", None, "manage"),
    ]


def test_every_new_route_asks_for_the_right_right(setting: dict[str, Any], fake_plex: FakePlex,
                                                  fake_jellyfin: FakeJellyfin) -> None:
    playlist, jelly, owner = setting["playlist"], setting["jelly"], setting["owner"]
    seen = Seen()
    for name in ("owner", "reader", "editor", "stranger", "operator"):
        seen.watch(setting[name])
    made = owner.post(f"/api/v1/playlists/{playlist}/mirrors", json={"server": jelly})
    assert made.status_code == 201, made.text
    sync(fake_plex)
    own = next(key for key, one in fake_jellyfin.tracks.items() if one.name == "Eisblume")
    fake_jellyfin.list_add(mirror_key(playlist, jelly), [own])
    sync(fake_plex, look_at_plex=True)
    extra = extras(playlist, jelly)[0].id
    entry = owner.get(f"/api/v1/playlists/{playlist}").json()["items"][0]["id"]
    code = invite(owner, playlist)
    guest = phone(code)
    odd: list[str] = []
    for method, url, body, need in _routes(playlist, jelly, entry, extra):
        for who in ("stranger", "reader", "editor"):
            status = setting[who].request(method, url, json=body).status_code
            wanted = {"stranger": (404,),
                      "reader": (200,) if need == "view" else (403,),
                      "editor": (200, 204) if need in ("view", "edit") else (403,)}[who]
            if status not in wanted:
                odd.append(f"{who} {method} {url} -> {status}")
        if guest.request(method, url, json=body).status_code not in (401, 403):
            odd.append(f"guest {method} {url}")
    assert odd == []
    # The operator and the owner may; the operator alone takes all over and lets a server go.
    assert setting["operator"].post(f"/api/v1/playlists/{playlist}/mirrors/preview",
                                    json={"server": jelly}).status_code == 200
    for who in ("owner", "editor", "reader"):
        assert setting[who].post(f"/api/v1/servers/{jelly}/transfer/preview",
                                 json={"mode": "mirror"}).status_code == 403
        assert setting[who].delete("/api/v1/servers/1").status_code == 403
    assert setting["operator"].post(f"/api/v1/servers/{jelly}/transfer/preview",
                                    json={"mode": "move"}).status_code == 200
    assert setting["operator"].get(f"/api/v1/servers/{jelly}/transfer").status_code == 200
    assert setting["owner"].post(f"/api/v1/playlists/{playlist}/move", json={"server": jelly}).status_code == 200
    # Not one of Jellyfin's ids, its address or a token in any answer.
    forbidden = secrets_of(fake_jellyfin)
    leaks = [(where, value) for where, text in seen.answers for value in forbidden if value in text]
    assert leaks == []


def test_choices_name_only_tracks_of_the_target(setting: dict[str, Any]) -> None:
    playlist, jelly, owner = setting["playlist"], setting["jelly"], setting["owner"]
    # A track of Plex is no track of Jellyfin: the choice is not taken.
    answer = owner.post(f"/api/v1/playlists/{playlist}/mirrors/preview",
                        json={"server": jelly, "choices": {str(plex_numbers([3021])[0]): plex_numbers([3001])[0]}})
    assert answer.status_code == 200 and answer.json()["missing"] == 1
    assert owner.post(f"/api/v1/playlists/{playlist}/mirrors/preview",
                      json={"server": 999}).status_code == 404
    assert owner.post(f"/api/v1/playlists/{playlist}/mirrors/preview",
                      json={"server": jelly, "choices": {"x": 1}}).status_code == 422
    assert owner.post(f"/api/v1/playlists/{playlist}/move",
                      json={"server": jelly, "old": "burn"}).status_code == 422
