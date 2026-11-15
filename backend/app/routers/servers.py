"""The media servers, as everybody signed in may see them: which there are, what they are called, whether they are
connected and ready, and what they can do. Never where they are or how nexmix signs in there. Connecting a server goes
through ``/api/v1/plex`` and ``/api/v1/jellyfin`` (the operator); letting go of any one of them through here."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response

from ..deps import DbSession, OperatorAccount, Reader
from ..errors import error
from ..models import SERVER_JELLYFIN
from ..services import jellyfin_link, media, plex_link, sources

router = APIRouter(prefix="/api/v1/servers", tags=["servers"])


@router.get("", summary="The media servers: name, kind, connected or resting, ready, and what each can do")
def servers(_reader: Reader, db: DbSession) -> dict[str, Any]:
    first = media.primary(db)
    return {
        "servers": [{**media.view(row), "capabilities": sources.capabilities(row.kind).view(),
                     "primary": first is not None and row.id == first.id} for row in media.every(db)],
    }


@router.delete("/{server_id}", status_code=204,
               summary="Disconnect a media server: its token and its catalogue go; the server itself stays as it is, "
                       "and the playlists that take their tracks from it wait for it in nexmix")
def disconnect(server_id: Annotated[int, Path(ge=1, le=10**9)], _operator: OperatorAccount,
               db: DbSession) -> Response:
    row = media.get(db, server_id)
    if row is None or not row.active:
        raise error("not_found", "Not found.", 404)
    if row.kind == SERVER_JELLYFIN:
        jellyfin_link.rest(db, row)
    else:
        plex_link.rest(db, row)
    db.commit()
    return Response(status_code=204)
