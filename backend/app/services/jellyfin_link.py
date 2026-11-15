"""Connecting a Jellyfin server, for the operator: its address, then Quick Connect (a code confirmed in Jellyfin, as
the PIN at plex.tv; no password passes through nexmix) or an API key typed by hand; then the music library and the
Jellyfin user the playlists will belong to. Each Jellyfin server is a row of ``media_servers`` of its own, beside the
servers already there: nothing moves to it.

**The token** is sealed with the server secret in a context of its own per server (``jellyfin:<row>``) the moment it
is stored; it is never in an answer, never in the log, and the browser never sees it. A token of Quick Connect belongs
to the Jellyfin user who confirmed the code.

**One device id per installation and server** (``device_id``): Jellyfin keeps one token per device id, and a new
sign-in with the same id ends the token before. So connecting the same server again replaces nexmix's own token, and
nobody else's.

**Quick Connect under way** lives in memory only, for ten minutes at most, and belongs to the session that began it:
the secret that redeems the code never leaves the server, is redeemed once and forgotten. Jellyfin would let it be
redeemed again within the ten minutes, by anybody who had it.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .. import clock
from ..models import SERVER_JELLYFIN, MediaServer
from ..security import decrypt_secret, encrypt_secret
from . import media, plex_link, settings_service
from .jellyfin import JellyfinError, JellyfinServer, Library, User, check_address

logger = logging.getLogger("nexmix.jellyfin")

#: A code of Quick Connect is good for ten minutes in Jellyfin.
QUICK_SECONDS = 600
PROBE_SECONDS = 10.0
#: The longest API key taken (Jellyfin's are 32 characters).
KEY_LENGTH = 128

Unreadable = plex_link.Unreadable


def _context(row: MediaServer) -> str:
    return f"jellyfin:{row.id}"


def token_of(row: MediaServer) -> str:
    """The token, unsealed; ``Unreadable`` when there is none or it cannot be opened (another server secret)."""
    try:
        token = decrypt_secret(row.token_enc, _context(row))
    except Exception as exc:
        raise Unreadable from exc
    if not token:
        raise Unreadable
    return token


def device_id(db: Session, server: str) -> str:
    """nexmix's own device id at a Jellyfin server: the same for this installation and that server, always."""
    base = str(settings_service.get(db, "jellyfin_device") or "")
    if not base:
        made = secrets.token_hex(8)
        settings_service.save(db, {"jellyfin_device": made})
        base = str(settings_service.get(db, "jellyfin_device") or made)
    return f"nexmix-{base}-{hashlib.sha256(server.encode()).hexdigest()[:10]}"


def open_row(db: Session, row: MediaServer, **options: object) -> JellyfinServer:
    return JellyfinServer(row.url, token_of(row), device_id(db, row.machine_id), **options)  # type: ignore[arg-type]


# --- Quick Connect ----------------------------------------------------------------------------------------------------


@dataclass
class Pending:
    owner: str
    url: str
    server: str
    name: str
    device: str
    secret: str
    code: str
    started: float
    lock: threading.Lock = field(default_factory=threading.Lock)


_pending: dict[str, Pending] = {}
_pending_lock = threading.Lock()


def _sweep() -> None:
    moment = clock.monotonic()
    with _pending_lock:
        for key in [key for key, value in _pending.items() if moment - value.started > QUICK_SECONDS]:
            del _pending[key]


def forget() -> None:
    with _pending_lock:
        _pending.clear()


def _own(ident: str, owner: str) -> Pending | None:
    _sweep()
    with _pending_lock:
        found = _pending.get(ident)
    return found if found is not None and secrets.compare_digest(found.owner, owner) else None


def left(pending: Pending) -> int:
    return max(0, int(QUICK_SECONDS - (clock.monotonic() - pending.started)))


def probe(db: Session, url: str) -> tuple[str, dict[str, str]]:
    """The address checked and the server's name, id and version (``/System/Info/Public``, no sign-in)."""
    address = check_address(url)
    with JellyfinServer(address, "", "nexmix-probe", timeout=PROBE_SECONDS) as server:
        info = server.public_info()
    return address, info


def begin(db: Session, url: str, owner: str) -> tuple[str, Pending]:
    """A code of Quick Connect for the server at ``url``, to be confirmed in Jellyfin."""
    address, info = probe(db, url)
    device = device_id(db, info["id"])
    with JellyfinServer(address, "", device, timeout=PROBE_SECONDS) as server:
        try:
            started = server.quick_begin()
        except JellyfinError as exc:
            if exc.code in ("jellyfin_refused", "jellyfin_bad_request", "jellyfin_error", "jellyfin_not_found"):
                # Quick Connect is switched off in that Jellyfin: the API key is left.
                raise JellyfinError("quick_off", 409) from exc
            raise
    ident = secrets.token_hex(12)
    pending = Pending(owner=owner, url=address, server=info["id"], name=info["name"], device=device,
                      secret=started["secret"], code=started["code"], started=clock.monotonic())
    _sweep()
    with _pending_lock:
        _pending[ident] = pending
    return ident, pending


def state(ident: str, owner: str) -> tuple[Pending, bool]:
    """The Quick Connect under way and whether its code was confirmed."""
    pending = _own(ident, owner)
    if pending is None:
        raise JellyfinError("quick_expired", 404)
    with pending.lock, JellyfinServer(pending.url, "", pending.device, timeout=PROBE_SECONDS) as server:
        try:
            done = server.quick_done(pending.secret)
        except JellyfinError as exc:
            if exc.code == "quick_expired":
                cancel(ident, owner)
            raise
    return pending, done


def cancel(ident: str, owner: str) -> None:
    if _own(ident, owner) is not None:
        with _pending_lock:
            _pending.pop(ident, None)


@dataclass
class Found:
    """A server signed in to: where, who it is, the token, whose it is, and what may be chosen."""

    url: str
    server: str
    name: str
    token: str
    via: str
    #: The user the token belongs to; None for an API key.
    user: User | None
    libraries: list[Library]
    users: list[User]


def _look(db: Session, url: str, server_id: str, name: str, token: str, via: str) -> Found:
    with JellyfinServer(url, token, device_id(db, server_id), timeout=PROBE_SECONDS * 2) as server:
        user = server.me()
        admin = user is None or user.admin
        users = server.users() if admin else [user] if user is not None else []
        libraries = server.libraries(None if user is None else user.id, admin)
    if user is not None and not user.admin:
        # A user who is no administrator may make playlists for himself only.
        users = [user]
    return Found(url, server_id, name, token, via, user, libraries, users)


def redeem(db: Session, ident: str, owner: str) -> Found:
    """The confirmed code redeemed (once): the token of the user who confirmed it. The secret is forgotten."""
    pending = _own(ident, owner)
    if pending is None:
        raise JellyfinError("quick_expired", 404)
    with _pending_lock:
        _pending.pop(ident, None)
    with JellyfinServer(pending.url, "", pending.device, timeout=PROBE_SECONDS) as server:
        token = server.quick_redeem(pending.secret)
    return _look(db, pending.url, pending.server, pending.name, token, "quick")


def by_key(db: Session, url: str, key: str) -> Found:
    """An API key typed by the operator (Jellyfin › Dashboard › API keys), checked by asking the server."""
    key = (key or "").strip()
    if not key or len(key) > KEY_LENGTH or any(char.isspace() or char in '"\\' for char in key):
        raise JellyfinError("bad_token", 422)
    address, info = probe(db, url)
    return _look(db, address, info["id"], info["name"], key, "key")


# --- Keeping it -------------------------------------------------------------------------------------------------------


def store(db: Session, found: Found) -> MediaServer:
    """Keeps the server beside the others; one connected before is taken up again with its numbers. The library and
    the user are chosen at once when there is only one of each; the catalogue is fetched anew."""
    row = media.by_machine(db, SERVER_JELLYFIN, found.server)
    if row is None:
        row = MediaServer(kind=SERVER_JELLYFIN, serial=1, machine_id=found.server, generation=0, marks={})
        db.add(row)
        db.flush()
    else:
        media.drop_catalog(db, row.id)
        row.serial += 1
    row.active, row.via, row.name, row.owned, row.url = True, found.via, found.name, True, found.url
    row.token_enc = encrypt_secret(found.token, _context(row))
    row.connected_at, row.generation, row.marks, row.synced_at, row.full_at = clock.now(), 0, {}, None, None
    row.analysis_enabled, row.analysis_sampled, row.analysis_found, row.analysis_at = None, 0, 0, None
    if not any(library.key == row.section_key for library in found.libraries):
        row.section_key, row.section_title = "", ""
    if not row.section_key and len(found.libraries) == 1:
        row.section_key, row.section_title = found.libraries[0].key, found.libraries[0].title
    if not any(user.id == row.user_id for user in found.users):
        row.user_id, row.user_name = "", ""
    if not row.user_id and len(found.users) == 1:
        row.user_id, row.user_name = found.users[0].id, found.users[0].name
    db.flush()
    logger.info("Jellyfin connected via=%s libraries=%s users=%s server=%s", found.via, len(found.libraries),
                len(found.users), row.id)
    return row


def libraries(db: Session, row: MediaServer) -> tuple[list[Library], list[User]]:
    """What may be chosen now: the music libraries and the users."""
    with open_row(db, row, timeout=PROBE_SECONDS * 2) as server:
        user = server.me()
        admin = user is None or user.admin
        users = server.users() if admin else [user] if user is not None else []
        found = server.libraries(None if user is None else user.id, admin)
    if user is not None and not user.admin:
        users = [user]
    return found, users


def choose_library(db: Session, row: MediaServer, library: Library) -> None:
    """Another library: the catalogue of the old one goes, a new one is fetched."""
    media.drop_catalog(db, row.id)
    row.serial += 1
    row.section_key, row.section_title = library.key, library.title
    row.generation, row.marks, row.synced_at, row.full_at = 0, {}, None, None
    db.flush()


def choose_user(db: Session, row: MediaServer, user: User) -> None:
    row.user_id, row.user_name = user.id, user.name
    db.flush()


def rest(db: Session, row: MediaServer) -> None:
    """Let go of: the token and the catalogue go; its numbers, the chosen library and user stay for a later return."""
    plex_link.rest(db, row)
    logger.info("Jellyfin disconnected server=%s", row.id)
