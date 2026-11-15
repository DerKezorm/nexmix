"""The media servers, as everybody signed in may see them: which there are, what they are called, whether they are
connected and ready, and what they can do. Never where they are or how nexmix signs in there. Connecting a Plex server
goes through ``/api/v1/plex`` (the operator)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..deps import DbSession, Reader
from ..services import media, sources

router = APIRouter(prefix="/api/v1/servers", tags=["servers"])


@router.get("", summary="The media servers: name, kind, connected or resting, ready, and what each can do")
def servers(_reader: Reader, db: DbSession) -> dict[str, Any]:
    first = media.primary(db)
    return {
        "servers": [{**media.view(row), "capabilities": sources.capabilities(row.kind).view(),
                     "primary": first is not None and row.id == first.id} for row in media.every(db)],
    }
