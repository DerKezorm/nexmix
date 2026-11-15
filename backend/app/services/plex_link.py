"""The Plex server of the operator's Plex card: signing in through plex.tv, picking the server and its library, the
address and token by hand, and letting go of it. The server is a row of ``media_servers`` (``services/media.py``);
connecting another one in its place moves the playlists there (their entries are found on it, ``services/sync.py``),
and the one before rests with its copies of the playlists, to be taken up again when it is connected once more.

The token is sealed with the server secret (``security.encrypt_secret``, context ``plex``) the moment it is stored;
it is never in an answer, never in the log, and the browser never sees it. Sign-ins under way live in memory only,
for ten minutes at most (the servers found there carry their tokens), and belong to the session that began them:
another one, even of another operator, neither sees them nor takes their server.
"""

from __future__ import annotations

import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import clock
from ..models import SERVER_PLEX, MediaServer, Playlist, ServerPlaylist
from ..security import decrypt_secret, encrypt_secret
from . import media, plextv, settings_service
from .plex import PlexError, PlexServer, Section, check_address

logger = logging.getLogger("nexmix.plex")

#: A sign-in waits this long for the operator at Plex, then it is over.
PIN_SECONDS = 600
#: How long one address of a server may take to answer while the right one is looked for.
PROBE_SECONDS = 6.0
_CONTEXT = "plex"


class Unreadable(Exception):
    """The stored token cannot be opened (the server secret changed)."""


def client_id(db: Session) -> str:
    """The one identifier nexmix gives itself at Plex: Plex keeps a device entry for every one it sees."""
    known = str(settings_service.get(db, "plex_client_id") or "")
    if known:
        return known
    made = "nexmix-" + secrets.token_hex(12)
    settings_service.save(db, {"plex_client_id": made})
    return str(settings_service.get(db, "plex_client_id") or made)


def current(db: Session) -> MediaServer | None:
    """The Plex server the Plex card shows: the first one connected."""
    return media.primary(db, SERVER_PLEX)


def token_of(row: MediaServer) -> str:
    try:
        return decrypt_secret(row.token_enc, _CONTEXT)
    except Exception as exc:
        raise Unreadable from exc


def server_of(db: Session, row: MediaServer, **options: Any) -> PlexServer:
    return PlexServer(row.url, token_of(row), client_id(db), **options)


# --- Sign-in through plex.tv ------------------------------------------------------------------------------------------


@dataclass
class Found:
    """A server the account may use that answered, with the address that answered and its music libraries."""

    name: str
    owned: bool
    machine_id: str
    token: str
    url: str
    sections: list[Section]


@dataclass
class Pending:
    code: str
    started: float
    #: The session that began the sign-in: only it may ask for its state, take its server or give it up.
    owner: str
    #: The servers once the token is there; None while the operator is still at Plex.
    servers: list[Found] | None = None
    #: How many servers the account has at all (to tell "none with music" from "none").
    offered: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_pending: dict[str, Pending] = {}
_pending_lock = threading.Lock()


def _sweep() -> None:
    moment = clock.monotonic()
    with _pending_lock:
        for key in [key for key, value in _pending.items() if moment - value.started > PIN_SECONDS]:
            del _pending[key]


def forget() -> None:
    with _pending_lock:
        _pending.clear()


def _own(pin_id: str, owner: str) -> Pending | None:
    """The sign-in under way, when it belongs to ``owner``; for anybody else there is none."""
    with _pending_lock:
        pending = _pending.get(pin_id)
    return pending if pending is not None and secrets.compare_digest(pending.owner, owner) else None


def begin(db: Session, owner: str) -> dict[str, str]:
    _sweep()
    try:
        pin = plextv.begin(client_id(db))
    except plextv.PlexTvError as exc:
        raise PlexError(exc.code) from exc
    with _pending_lock:
        _pending[pin["id"]] = Pending(code=pin["code"], started=clock.monotonic(), owner=owner)
    return {"id": pin["id"], "url": pin["url"]}


def cancel(pin_id: str, owner: str) -> None:
    """A sign-in given up: what it found (servers with their tokens) goes now, not after ten minutes."""
    if _own(pin_id, owner) is not None:
        finish(pin_id)


def _probe(server: plextv.Server, own_id: str) -> Found | None:
    """The first address of the server that answers as that server, and its music libraries. An address that
    answers as another server (a reused local address) does not count."""
    for connection in server.connections:
        try:
            url = check_address(connection.uri)
            with PlexServer(url, server.token, own_id, timeout=PROBE_SECONDS) as candidate:
                identity = candidate.identity()
                if server.machine_id and identity["machine_id"] != server.machine_id:
                    continue
                sections = candidate.music_sections()
        except PlexError:
            continue
        return Found(server.name, server.owned, server.machine_id, server.token, url, sections)
    return None


def poll(db: Session, pin_id: str, owner: str) -> Pending:
    """Asks plex.tv whether the operator agreed; then finds the servers with music, each at an address that answers."""
    _sweep()
    pending = _own(pin_id, owner)
    if pending is None:
        raise PlexError("pin_expired", 404)
    own_id = client_id(db)
    with pending.lock:
        if pending.servers is not None:
            return pending
        try:
            token = plextv.poll(own_id, pin_id, pending.code)
        except plextv.PlexTvError as exc:
            raise PlexError(exc.code, 404 if exc.code == "pin_expired" else 502) from exc
        if token is None:
            return pending
        try:
            offered = plextv.servers(own_id, token)
        except plextv.PlexTvError as exc:
            raise PlexError(exc.code) from exc
        with ThreadPoolExecutor(max_workers=4) as pool:
            probed = list(pool.map(lambda server: _probe(server, own_id), offered))
        pending.offered = len(offered)
        pending.servers = [one for one in probed if one is not None and one.sections]
    return pending


def chosen(pin_id: str, index: int, owner: str) -> Found:
    _sweep()
    pending = _own(pin_id, owner)
    if pending is None or pending.servers is None or not 0 <= index < len(pending.servers):
        raise PlexError("pin_expired", 404)
    return pending.servers[index]


def finish(pin_id: str) -> None:
    with _pending_lock:
        _pending.pop(pin_id, None)


# --- Connecting -------------------------------------------------------------------------------------------------------


def by_hand(db: Session, url: str, token: str) -> Found:
    """The address and token typed by the operator: checked by asking the server who it is."""
    address = check_address(url)
    token = token.strip()
    if not token or len(token) > 300 or any(char.isspace() for char in token):
        raise PlexError("bad_token", 422)
    with PlexServer(address, token, client_id(db), timeout=PROBE_SECONDS * 2) as server:
        identity = server.identity()
        sections = server.music_sections()
    return Found(identity["name"] or "Plex", True, identity["machine_id"], token, address, sections)


def _resting(db: Session) -> MediaServer | None:
    """The Plex server connected last, now resting: connecting a server makes it the place of that one's playlists."""
    return db.scalars(select(MediaServer).where(MediaServer.kind == SERVER_PLEX, MediaServer.active.is_(False))
                      .order_by(MediaServer.connected_at.desc(), MediaServer.id.desc()).limit(1)).first()


def store(db: Session, found: Found, via: str, *, alongside: bool = False) -> MediaServer:
    """Keeps the server (and the library, when it has exactly one); its catalogue is fetched anew. A server connected
    before is taken up again with its numbers and its copies of the playlists. Another server in the place of the one
    before: that one rests, and its playlists come here (``sync.moved``). ``alongside``: a server more, beside the ones
    connected; nothing moves."""
    from . import sync

    before = None if alongside else current(db) or _resting(db)
    row = media.by_machine(db, SERVER_PLEX, found.machine_id)
    if row is None:
        row = MediaServer(kind=SERVER_PLEX, serial=1, machine_id=found.machine_id, generation=0, marks={})
        db.add(row)
        db.flush()
    else:
        media.drop_catalog(db, row.id)
        row.serial += 1
    row.active, row.via, row.name, row.owned, row.url = True, via, found.name, found.owned, found.url
    row.token_enc = encrypt_secret(found.token, _CONTEXT)
    row.connected_at, row.generation, row.marks, row.synced_at, row.full_at = clock.now(), 0, {}, None, None
    row.analysis_enabled, row.analysis_sampled, row.analysis_found, row.analysis_at = None, 0, 0, None
    row.section_key, row.section_title = "", ""
    if len(found.sections) == 1:
        row.section_key, row.section_title = found.sections[0].key, found.sections[0].title
    db.flush()
    if before is not None and before.id != row.id:
        if before.active:
            rest(db, before)
        # Its copies are taken up again only once their server is back and still lists them.
        db.execute(update(ServerPlaylist).where(ServerPlaylist.server_id == before.id).values(resume=True))
        moving = list(db.scalars(select(Playlist.id).where(
            (Playlist.source_server_id == before.id) | Playlist.source_server_id.is_(None))))
        sync.moved(db, moving, row.id)
    elif not alongside:
        sync.moved(db, list(db.scalars(select(Playlist.id).where(Playlist.source_server_id.is_(None)))), row.id,
                   settle="since")
    logger.info("Plex connected via=%s owned=%s libraries=%s server=%s", via, found.owned, len(found.sections), row.id)
    return row


def choose_library(db: Session, row: MediaServer, section: Section) -> None:
    """Another library: the catalogue of the old one goes, a new one is fetched."""
    media.drop_catalog(db, row.id)
    row.serial += 1
    row.section_key, row.section_title = section.key, section.title
    row.generation, row.marks, row.synced_at, row.full_at = 0, {}, None, None
    row.analysis_enabled, row.analysis_sampled, row.analysis_found, row.analysis_at = None, 0, 0, None
    db.flush()


def rest(db: Session, row: MediaServer) -> None:
    """The server let go of: its token and its catalogue go, its numbers and its copies of the playlists stay."""
    media.drop_catalog(db, row.id)
    row.active, row.token_enc, row.url, row.serial = False, "", "", row.serial + 1
    row.generation, row.marks, row.synced_at, row.full_at = 0, {}, None, None
    row.analysis_enabled, row.analysis_sampled, row.analysis_found, row.analysis_at = None, 0, 0, None
    db.flush()


def disconnect(db: Session) -> None:
    row = current(db)
    if row is not None:
        rest(db, row)
    logger.info("Plex disconnected")


def started_ago(pending: Pending) -> float:
    return max(0.0, clock.monotonic() - pending.started)
