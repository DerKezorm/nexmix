"""Signing in with Plex: plex.tv hands out the token, nobody copies it from anywhere.

Plex separates two things. plex.tv answers "who are you" and "which servers may you use"; the Plex Media Server
answers "what is in the library". The sign-in runs over a PIN: nexmix asks plex.tv for one, the operator's browser
opens Plex's own page with it in a new window, and nexmix asks plex.tv until the token is there.

Of the servers the account may use (``/resources``), only servers count, own and shared ones; each comes with its
addresses. ``connection_order`` puts them in the order they are tried: local before remote, a secure address before a
plain one, a relay only when nothing else answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from . import outbound
from .plex import PRODUCT

#: The tests put a stand-in plex.tv here; None: the network.
TRANSPORT: httpx.BaseTransport | None = None


class PlexTvError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Connection:
    uri: str
    local: bool
    relay: bool
    secure: bool


@dataclass
class Server:
    name: str
    owned: bool
    machine_id: str
    #: The token for this server: a shared server has its own, the account's for an own one. Never in an answer.
    token: str
    connections: list[Connection] = field(default_factory=list)


def _headers(client_id: str, token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Device": PRODUCT,
        "X-Plex-Platform": "Web",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def _request(method: str, path: str, client_id: str, token: str | None = None,
             params: dict[str, Any] | None = None) -> Any:
    base = get_settings().plex_tv_url.rstrip("/")
    try:
        with httpx.Client(transport=TRANSPORT, verify=outbound.tls(), trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(15.0, connect=8.0)) as client:
            answer = client.request(method, f"{base}/api/v2{path}", headers=_headers(client_id, token), params=params)
    except httpx.TimeoutException as exc:
        raise PlexTvError("plextv_unreachable") from exc
    except httpx.HTTPError as exc:
        raise PlexTvError("plextv_unreachable") from exc
    if answer.status_code in (401, 403):
        raise PlexTvError("plextv_refused")
    if answer.status_code == 404:
        raise PlexTvError("pin_expired")
    if answer.status_code >= 300:
        raise PlexTvError("plextv_error")
    if not answer.content:
        return None
    try:
        return answer.json()
    except ValueError as exc:
        raise PlexTvError("plextv_error") from exc


def begin(client_id: str) -> dict[str, str]:
    """A PIN and the page of Plex where the operator agrees to it."""
    data = _request("POST", "/pins", client_id, params={"strong": "true"})
    pin_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
    code = str((data or {}).get("code") or "") if isinstance(data, dict) else ""
    if not pin_id.isdigit() or not code:
        raise PlexTvError("plextv_error")
    query = urlencode({"clientID": client_id, "code": code, "context[device][product]": PRODUCT})
    return {"id": pin_id, "code": code, "url": f"{get_settings().plex_auth_url}#?{query}"}


def poll(client_id: str, pin_id: str, code: str) -> str | None:
    """The token once the operator agreed at Plex; None while the window is still open."""
    data = _request("GET", f"/pins/{pin_id}", client_id, params={"code": code})
    token = data.get("authToken") if isinstance(data, dict) else None
    return str(token) if token else None


def connection_order(connections: list[Connection]) -> list[Connection]:
    """Local before remote, secure before plain, a relay last."""
    return sorted(connections, key=lambda one: (one.relay, not one.local, not one.secure))


def servers(client_id: str, token: str) -> list[Server]:
    data = _request("GET", "/resources", client_id, token=token, params={"includeHttps": "1", "includeRelay": "1"})
    found: list[Server] = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict) or "server" not in str(entry.get("provides") or ""):
            continue
        connections = []
        for one in entry.get("connections") or []:
            if not isinstance(one, dict) or not one.get("uri"):
                continue
            uri = str(one["uri"])
            connections.append(Connection(uri=uri, local=bool(one.get("local")), relay=bool(one.get("relay")),
                                          secure=uri.startswith("https://")))
        found.append(Server(
            name=str(entry.get("name") or "Plex").strip()[:200],
            owned=bool(entry.get("owned")),
            machine_id=str(entry.get("clientIdentifier") or ""),
            token=str(entry.get("accessToken") or "") or token,
            connections=connection_order(connections),
        ))
    found.sort(key=lambda server: (not server.owned, server.name.lower()))
    return found
