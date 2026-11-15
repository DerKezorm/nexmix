"""Listening in under ``/api/v1/preview``: about thirty seconds of a track of the catalogue, from a good third of it,
passed through nexmix (``services/preview.py``). The Plex token stays on the server; the browser names only the track.

Who may listen is decided by ``preview.may_listen`` at each request (from the start: a signed-in account, with a
session or a device token), then the operator's switch, then whether the track is in the catalogue."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.security import HTTPAuthorizationCredentials
from starlette.responses import Response

from ..deps import bearer_scheme
from ..services import preview
from ..services.media import NUMBER_LIMIT

router = APIRouter(prefix="/api/v1/preview", tags=["preview"])

Scheme = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


@router.get("", summary="Whether listening in is on, and how many previews are left this hour")
async def preview_status(request: Request, _scheme: Scheme = None) -> dict[str, Any]:
    return await asyncio.to_thread(preview.status, request)


@router.get(
    "/{track_id}",
    response_class=Response,
    summary="About 30 seconds of a track as MP3, from 35 % of it (one at a time per listener, 120 an hour)",
    responses={200: {"content": {"audio/mpeg": {}}, "description": "The sound, as it comes"},
               206: {"content": {"audio/mpeg": {}}, "description": "The part a Range header asks for, with its length"},
               416: {"description": "The part lies beyond the end of the preview"}},
)
async def preview_track(track_id: Annotated[int, Path(ge=1, le=NUMBER_LIMIT)], request: Request,
                        _scheme: Scheme = None) -> Response:
    plan = await asyncio.to_thread(preview.prepare, request, track_id)
    return await preview.start(plan, request.headers.get("range"), request.is_disconnected)
