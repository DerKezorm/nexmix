"""A backup holds everything nexmix keeps that it cannot fetch again, and a restore brings it back: the playlists with
their entries and who added them, the shares, the trash, the last state shared with Plex (the base of the sync), the
connection to Plex with its token still readable, the invitations for guests with their guests, and what an account
asked not to see again (suggestions, playlists hidden from its overview, the smart playlists of Plex shown or not). The catalogue may come along; it is fetched from Plex anyway.

The Plex token is in the archive only sealed: not one byte of it in the clear, in any entry."""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models
from app.db import SessionLocal
from app.models import Account
from app.services import backups, plex_link
from app.services import sync as server_sync

from .conftest import make_account, new_client
from .fakeplex import FakePlex
from .plex_world import both_equal, connect, make, plex_key, sync
from .test_guests_and_plex import invite, phone

#: Every table whose rows a restore must bring back (the catalogue and its runs are fetched again and may be missing).
KEPT_TABLES = {
    "settings", "users", "oidc_providers", "oidc_links", "passkeys", "invites", "api_keys", "devices", "push_devices",
    "media_servers", "server_items", "playlists", "playlist_items", "playlist_shares", "hidden_suggestions",
    "guest_invites", "guests", "guest_entries", "server_playlists", "hidden_playlists", "mirror_items", "mirror_extras",
}
#: Tables a backup may carry but need not: made again (the catalogue), or worth nothing after a restore (sign-ins,
#: one-time links, the queue of work).
FETCHED_OR_PASSING = {
    "artists", "albums", "tracks", "tags", "track_tags", "album_tags", "artist_tags", "sync_runs", "auth_sessions",
    "password_resets", "email_confirmations", "jobs", "transfers",
}


@pytest.fixture(autouse=True)
def no_leftovers() -> Iterator[None]:
    shutil.rmtree(backups.pending_folder(), ignore_errors=True)
    yield
    shutil.rmtree(backups.pending_folder(), ignore_errors=True)


def test_every_table_is_named_as_kept_or_as_fetched_again() -> None:
    """A new table must be decided on here: does a restore have to bring it back?"""
    tables = set(models.Base.metadata.tables)
    assert tables - KEPT_TABLES - FETCHED_OR_PASSING == set(), "a new table: kept by a backup, or fetched again?"
    assert (KEPT_TABLES | FETCHED_OR_PASSING) - tables == set()


def _counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {name: db.scalar(select(func.count()).select_from(models.Base.metadata.tables[name])) or 0
                for name in sorted(KEPT_TABLES)}


def _state(client: TestClient, playlist_id: int) -> dict[str, Any]:
    view = client.get(f"/api/v1/playlists/{playlist_id}").json()
    return {"name": view["name"], "items": [(item["track"], item.get("by", {}).get("name") if isinstance(
        item.get("by"), dict) else item.get("by")) for item in view["items"]]}


def test_a_restore_brings_back_playlists_shares_the_sync_base_plex_and_the_guests(
        client: TestClient, operator: Account, fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    server_sync.forget()
    playlist = make(client, "Gartenfest", [3001, 3003, 3013])
    sync(fake_plex)
    key = plex_key(playlist["id"])
    member = make_account("mia")
    assert client.put(f"/api/v1/playlists/{playlist['id']}/shares/{member.id}",
                      json={"access": "edit"}).status_code == 200
    lena = phone(invite(client, playlist["id"]))
    assert lena.post("/api/v1/guest/items", json={"tracks": [3026]}).status_code == 200
    sync(fake_plex)
    assert both_equal(fake_plex, playlist["id"]) == [3001, 3003, 3013, 3026]
    assert client.post("/api/v1/suggestions/hidden", json={"track": 3003}).status_code in (200, 201, 204)
    trashed = make(client, "Alte Liste", [3001])
    assert client.delete(f"/api/v1/playlists/{trashed['id']}").status_code == 200
    # Hidden from the own view, and the smart playlists of Plex shown.
    assert client.put(f"/api/v1/playlists/{playlist['id']}/hidden").status_code == 200
    assert client.put("/api/v1/me/preferences", json={"show_smart_playlists": True}).status_code == 200
    before = _counts()
    assert all(before[name] > 0 for name in ("playlists", "playlist_items", "playlist_shares", "hidden_suggestions",
                                              "guest_invites", "guests", "guest_entries", "server_playlists",
                                              "media_servers", "server_items", "hidden_playlists")), before
    view_before = _state(client, playlist["id"])
    with SessionLocal() as db:
        base_before = {row.playlist_id: (row.ext_key, row.base) for row in db.scalars(select(models.ServerPlaylist))}

    name = backups.create(kind=backups.MANUAL).name

    # After the backup: the guest goes, the share goes, the hidden one is shown again, an entry goes, Plex is cut off.
    assert client.delete(f"/api/v1/playlists/{playlist['id']}/invite").status_code == 204
    assert client.delete(f"/api/v1/playlists/{playlist['id']}/shares/{member.id}").status_code == 200
    assert client.delete("/api/v1/suggestions/hidden/3003").status_code in (200, 204)
    assert client.post(f"/api/v1/playlists/{trashed['id']}/restore").status_code == 200
    assert client.delete(f"/api/v1/playlists/{playlist['id']}/hidden").status_code == 200
    assert client.put("/api/v1/me/preferences", json={"show_smart_playlists": False}).status_code == 200
    with SessionLocal() as db:
        db.query(models.ServerPlaylist).delete()
        db.query(models.MediaServer).delete()
        db.commit()
    assert lena.get("/api/v1/guest").status_code in (401, 403)

    backups.stage_restore(name)
    assert backups.apply_pending() is True

    assert _counts() == before
    with SessionLocal() as db:
        row = plex_link.current(db)
        assert row is not None and plex_link.token_of(row) == fake_plex.account_token, "the token can be read again"
        base_after = {one.playlist_id: (one.ext_key, one.base) for one in db.scalars(select(models.ServerPlaylist))}
        trash = db.get(models.Playlist, trashed["id"])
        assert trash is not None and trash.deleted_at is not None, "the trash is as it was"
    assert base_after == base_before and base_after[playlist["id"]][0] == str(key)
    # The sign-ins of the archive end at a restore; the people sign in again and find everything.
    with new_client(operator) as again:
        assert _state(again, playlist["id"]) == view_before
        assert again.get("/api/v1/suggestions/hidden").json()
        overview = again.get("/api/v1/playlists").json()
        assert overview["show_smart_playlists"] is True
        assert [card["hidden"] for card in overview["playlists"] if card["id"] == playlist["id"]] == [True]
        shares = again.get(f"/api/v1/playlists/{playlist['id']}/shares").json()
        assert [(one["person"]["id"], one["access"]) for one in shares["shares"]] == [(member.id, "edit")]
    # The guest's cookie and the invitation's code work again: the server's key came back with the archive.
    assert lena.get("/api/v1/guest").status_code == 200
    # The sync goes on from the base: nothing doubled, nothing lost.
    sync(fake_plex)
    assert both_equal(fake_plex, playlist["id"]) == [3001, 3003, 3013, 3026]


def test_the_plex_token_is_in_no_entry_of_the_archive_in_the_clear(client: TestClient, operator: Account,
                                                                 fake_plex: FakePlex) -> None:
    connect(client, fake_plex)
    token = fake_plex.account_token.encode()
    path = backups.path_of(backups.create(kind=backups.MANUAL).name)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert backups.DATABASE_ENTRY in names
        for entry in names:
            content = archive.read(entry)
            assert token not in content, entry
            assert token.hex().encode() not in content, entry
