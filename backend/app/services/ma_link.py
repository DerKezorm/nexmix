"""Connecting a Music Assistant server, for the operator: its address and a long-lived token made in Music Assistant
(profile › tokens), then the providers of its library whose tracks count. Each Music Assistant server is a row of
``media_servers`` of its own, beside the servers already there.

**The token** is sealed with the server secret in a context of its own per server (``music_assistant:<row>``) the
moment it is stored; it is never in an answer, never in the log, and the browser never sees it. Music Assistant's
"long-lived" tokens run out after a year (the ones made at its first start after 30 days): when it runs out is kept
(``token_expires_at``), shown on the card, and warned of ``WARN_DAYS`` before. A token run out or revoked makes every
request fail with ``ma_refused``: connect again with a new one.

**Providers.** Music Assistant puts the tracks of all its music providers into one library (a track that two providers
have is one track there). Only the chosen providers' tracks are nexmix's catalogue of this server. A provider that is a
Plex or Jellyfin server nexmix knows directly as well would bring the same tracks twice: the card says so
(``doubled``).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from .. import clock
from ..models import SERVER_JELLYFIN, SERVER_MA, SERVER_PLEX, MediaServer
from ..security import decrypt_secret, encrypt_secret
from . import media, music_assistant, plex_link
from .music_assistant import MaError

logger = logging.getLogger("nexmix.musicassistant")

#: The card warns this long before the token runs out.
WARN_DAYS = 30
#: The kinds of Music Assistant's providers that are other media servers nexmix may know directly.
DOUBLE_DOMAINS = {"plex": SERVER_PLEX, "jellyfin": SERVER_JELLYFIN}
#: Providers never offered: the built-in one holds the playlists and lists, no tracks of files.
NOT_OFFERED = frozenset({"builtin", "library"})

Unreadable = plex_link.Unreadable


def _context(row: MediaServer) -> str:
    return f"music_assistant:{row.id}"


def token_of(row: MediaServer) -> str:
    """The token, unsealed; ``Unreadable`` when there is none or it cannot be opened (another server secret)."""
    try:
        token = decrypt_secret(row.token_enc, _context(row))
    except Exception as exc:
        raise Unreadable from exc
    if not token:
        raise Unreadable
    return token


def connection(row: MediaServer) -> music_assistant.Connection:
    """The server's websocket, signed in (opened when needed)."""
    return music_assistant.connection(row.id, row.url, token_of(row))


@dataclass(frozen=True)
class Provider:
    instance: str
    domain: str
    name: str
    available: bool


def handle(row_or_server: MediaServer | str, key: str) -> str:
    """nexmix's own name for a provider of a server: the same each time, and no id of Music Assistant."""
    server = row_or_server if isinstance(row_or_server, str) else row_or_server.machine_id
    return hashlib.sha256(f"nexmix:ma:{server}:{key}".encode()).hexdigest()[:16]


def providers_of(found: Any) -> list[Provider]:
    """The music providers of an answer of ``providers``: of type ``music``, the built-in one left out."""
    chosen: list[Provider] = []
    for one in found if isinstance(found, list) else []:
        if not isinstance(one, dict) or one.get("type") != "music":
            continue
        instance = str(one.get("instance_id") or "")[:100]
        domain = str(one.get("domain") or "")[:60]
        if not instance or domain in NOT_OFFERED or instance in NOT_OFFERED:
            continue
        chosen.append(Provider(instance, domain, str(one.get("name") or domain).strip()[:200] or domain,
                               bool(one.get("available", True))))
    return chosen


@dataclass
class Found:
    """A server signed in to: where, who it is, the token, and its music providers."""

    url: str
    server: str
    name: str
    token: str
    providers: list[Provider]


def probe(url: str) -> tuple[str, dict[str, Any]]:
    address = music_assistant.check_address(url)
    return address, music_assistant.info(address)


def sign_in(url: str, token: str) -> Found:
    """The address checked (``/info``: a Music Assistant of a schema nexmix speaks), the token tried on a websocket of
    its own, the music providers read. ``bad_token`` for a token the server does not take, ``ma_not_admin`` for one of a
    user who may not run and cancel tasks."""
    token = music_assistant.check_token(token)
    address, server = probe(url)
    probe_connection = music_assistant.Connection(address, token)
    try:
        probe_connection.open()
    except MaError as exc:
        if exc.code == "ma_refused":
            raise MaError("bad_token", 422) from exc
        raise
    try:
        if not probe_connection.admin:
            # nexmix must be able to cancel a task it gave up on (``tasks/cancel``): never a task left to run later,
            # unseen, taking out by place what stands there by then.
            raise MaError(music_assistant.NOT_ADMIN, 422)
        found = probe_connection.call("providers", {}, timeout=15.0)
    finally:
        probe_connection.close()
    return Found(address, server["id"], server["name"], token, providers_of(found))


def store(db: Session, found: Found) -> MediaServer:
    """Keeps the server beside the others; one connected before is taken up again with its numbers. The providers it
    had stay chosen where they are still there; with one provider only, that one. The catalogue is fetched anew."""
    row = media.by_machine(db, SERVER_MA, found.server)
    if row is None:
        row = MediaServer(kind=SERVER_MA, serial=1, machine_id=found.server, generation=0, marks={})
        db.add(row)
        db.flush()
    else:
        media.drop_catalog(db, row.id)
        row.serial += 1
    music_assistant.drop(row.id)
    row.active, row.via, row.name, row.owned, row.url = True, "token", found.name, True, found.url
    row.token_enc = encrypt_secret(found.token, _context(row))
    row.token_expires_at = music_assistant.token_expiry(found.token)
    row.connected_at, row.generation, row.marks, row.synced_at, row.full_at = clock.now(), 0, {}, None, None
    row.analysis_enabled, row.analysis_sampled, row.analysis_found, row.analysis_at = None, 0, 0, None
    there = {one.instance for one in found.providers}
    kept = [one for one in (row.providers or []) if one in there]
    if not kept and len(found.providers) == 1:
        kept = [found.providers[0].instance]
    _choose(row, kept, found.providers)
    db.flush()
    logger.info("Music Assistant connected providers=%s chosen=%s server=%s", len(found.providers), len(kept), row.id)
    return row


def _choose(row: MediaServer, chosen: list[str], offered: list[Provider]) -> None:
    names = {one.instance: one.name for one in offered}
    row.providers = list(dict.fromkeys(chosen)) or None
    row.section_key = "library" if chosen else ""
    row.section_title = ", ".join(names.get(one, one) for one in chosen)[:200]


def choices(db: Session, row: MediaServer) -> list[Provider]:
    """The music providers that may be chosen now."""
    found = connection(row).call("providers", {}, timeout=15.0)
    return providers_of(found)


def choose(db: Session, row: MediaServer, chosen: list[str], offered: list[Provider]) -> bool:
    """Other providers: the catalogue goes and is fetched anew. False: the same as before."""
    wanted = list(dict.fromkeys(chosen))
    if sorted(wanted) == sorted(row.providers or []):
        return False
    media.drop_catalog(db, row.id)
    row.serial += 1
    row.generation, row.marks, row.synced_at, row.full_at = 0, {}, None, None
    _choose(row, wanted, offered)
    db.flush()
    return True


def doubled(db: Session, row: MediaServer, offered: list[Provider]) -> list[str]:
    """The chosen providers that are a Plex or Jellyfin server while nexmix knows such a server directly too: their
    tracks would stand twice. By the providers' names."""
    kinds = {server.kind for server in media.active(db) if server.id != row.id}
    chosen = set(row.providers or [])
    return [one.name for one in offered
            if one.instance in chosen and DOUBLE_DOMAINS.get(one.domain) in kinds]


def token_view(row: MediaServer, now: datetime | None = None) -> dict[str, Any]:
    """When the token runs out, how many days are left, and whether the card warns (``WARN_DAYS``) or it has run
    out."""
    moment = now or clock.now()
    expires = row.token_expires_at
    if expires is None:
        return {"expires_at": None, "days_left": None, "warn": False, "expired": False}
    left = expires - moment
    return {"expires_at": expires.isoformat(), "days_left": max(0, left.days),
            "warn": left <= timedelta(days=WARN_DAYS), "expired": left <= timedelta(0)}


def rest(db: Session, row: MediaServer) -> None:
    """Let go of: the token and the catalogue go, the websocket closes; its numbers and the chosen providers stay for a
    later return."""
    plex_link.rest(db, row)
    row.token_expires_at = None
    music_assistant.drop(row.id)
    logger.info("Music Assistant disconnected server=%s", row.id)
