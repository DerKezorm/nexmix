"""The way in for guests, open to everybody who has the link: ``/api/v1/join/{code}``.

Reading the code shows the playlist's name, its covers and who invites; joining with a name gives the browser the guest
cookie (``services/guests.GUEST_COOKIE``): HttpOnly, SameSite=Lax, ``Secure`` by the rules of the session cookie (only
over HTTPS, or a trusted proxy that says so), sent only to ``/api/v1``, and gone when the invitation ends. Every route
here goes through the brakes: a wrong code counts against its sender, a right one against its invitation."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import clock
from ..deps import DbSession
from ..services import guests
from . import catalog as catalog_routes
from .auth import secure_cookie

router = APIRouter(prefix="/api/v1/join", tags=["guests"])

Code = Annotated[str, Path(min_length=1, max_length=40)]


class JoinIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.get(
    "/{code}", summary="What a link shows before the name: the playlist, who invites, whether this browser joined")
def look(code: Code, request: Request, db: DbSession) -> dict[str, Any]:
    return guests.door(db, request, code)


def give_cookie(response: Response, request: Request, token: str, ends: datetime) -> None:
    """The guest cookie, to last as long as the invitation (and a minute)."""
    seconds = max(60, int((ends - clock.now()).total_seconds()) + 60)
    response.set_cookie(guests.GUEST_COOKIE, token, max_age=seconds, path=guests.COOKIE_PATH, httponly=True,
                        samesite="lax", secure=secure_cookie(request))


@router.post("/{code}", summary="Join with a name; the browser gets the guest cookie of this invitation")
def join(code: Code, payload: JoinIn, request: Request, response: Response, db: DbSession) -> dict[str, Any]:
    guest, token, ends = guests.join(db, request, code, payload.name)
    if token is not None:
        give_cookie(response, request, token, ends)
    return {"name": guest.name}


@router.get("/{code}/cover/{album_id}", summary="A cover of the playlist's mosaic, for the page of the link",
            response_model=None)
async def join_cover(
    code: Code,
    album_id: Annotated[int, Path(ge=1, le=10**12)],
    request: Request,
    db: DbSession,
    s: Annotated[int, Query(description="80, 200 or 400")] = 200,
) -> Response:
    await run_in_threadpool(guests.join_cover, db, request, code, album_id)
    return await catalog_routes.serve_cover(request, album_id, db, s)
