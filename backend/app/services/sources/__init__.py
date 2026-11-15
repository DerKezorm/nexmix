"""The connectors to media servers: one per kind of server, each behind the same narrow set of questions (the
catalogue, the playlists, a preview, the covers, "sounds alike", what it can do). The sync of the playlists, the
catalogue, the suggestions, the preview and the covers ask a connector; what is special about one kind of server stays
in its connector (``plex.py``, ``jellyfin.py``).

``open_server`` opens the connector of a server row; the token is unsealed for it and never leaves it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...models import SERVER_JELLYFIN, SERVER_PLEX, MediaServer
from .base import Capabilities, Entry, Listed, NotSupported
from .jellyfin import CAPABILITIES as JELLYFIN_CAPABILITIES
from .jellyfin import JellyfinSource
from .plex import CAPABILITIES as PLEX_CAPABILITIES
from .plex import PlexSource

__all__ = ["Capabilities", "Entry", "JellyfinSource", "Listed", "NotSupported", "PlexSource", "capabilities",
           "open_server", "secret_of"]

_CAPABILITIES: dict[str, Capabilities] = {SERVER_PLEX: PLEX_CAPABILITIES, SERVER_JELLYFIN: JELLYFIN_CAPABILITIES}


def capabilities(kind: str) -> Capabilities:
    return _CAPABILITIES[kind]


def secret_of(db: Session, row: MediaServer) -> tuple[str, str]:
    """The server's token and nexmix's own name there (Plex's client id, Jellyfin's device id), for a request built
    outside a connector (the sound of a preview). ``plex_link.Unreadable`` when the token cannot be opened."""
    from .. import jellyfin_link, plex_link

    if row.kind == SERVER_JELLYFIN:
        return jellyfin_link.token_of(row), jellyfin_link.device_id(db, row.machine_id)
    if row.kind == SERVER_PLEX:
        return plex_link.token_of(row), plex_link.client_id(db)
    raise NotSupported(row.kind)


def open_server(db: Session, row: MediaServer, **options: Any) -> PlexSource | JellyfinSource:
    """The connector of the server, opened (``with``). ``plex_link.Unreadable`` when its token cannot be opened. A
    Jellyfin server opened for a long piece of work (``stop`` given) waits while it starts, unless ``patient`` is
    False: the sync of the playlists never asks twice (a write whose answer was lost would be done twice) and waits
    with its own pauses."""
    from .. import jellyfin_link, plex_link

    if row.kind == SERVER_JELLYFIN:
        token, device = jellyfin_link.token_of(row), jellyfin_link.device_id(db, row.machine_id)
        if "stop" in options:
            options.setdefault("patient", True)
            options.setdefault("timeout", 60.0)
        return JellyfinSource(row.url, token, device, row.section_key, row.user_id or "", **options)
    if row.kind != SERVER_PLEX:
        raise NotSupported(row.kind)
    # Plex is never waited for inside its connector.
    options.pop("patient", None)
    return PlexSource(row.url, plex_link.token_of(row), plex_link.client_id(db), row.machine_id, row.section_key,
                      **options)
