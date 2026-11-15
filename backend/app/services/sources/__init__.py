"""The connectors to media servers: one per kind of server, each behind the same narrow set of questions (the
catalogue, the playlists, a preview, the covers, "sounds alike", what it can do). The sync of the playlists, the
catalogue, the suggestions, the preview and the covers ask a connector; what is special about one kind of server stays
in its connector (``plex.py``).

``open_server`` opens the connector of a server row; the token is unsealed for it and never leaves it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...models import SERVER_PLEX, MediaServer
from .base import Capabilities, Entry, Listed, NotSupported
from .plex import CAPABILITIES as PLEX_CAPABILITIES
from .plex import PlexSource

__all__ = ["Capabilities", "Entry", "Listed", "NotSupported", "PlexSource", "capabilities", "open_server"]

_CAPABILITIES: dict[str, Capabilities] = {SERVER_PLEX: PLEX_CAPABILITIES}


def capabilities(kind: str) -> Capabilities:
    return _CAPABILITIES[kind]


def open_server(db: Session, row: MediaServer, **options: Any) -> PlexSource:
    """The connector of the server, opened (``with``). ``plex_link.Unreadable`` when its token cannot be opened."""
    from .. import plex_link

    if row.kind != SERVER_PLEX:
        raise NotSupported(row.kind)
    return PlexSource(row.url, plex_link.token_of(row), plex_link.client_id(db), row.machine_id, row.section_key,
                      **options)
