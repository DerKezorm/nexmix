# nexmix

**Build playlists together from your Plex music library.**

nexmix is a small web app for a household: everybody gets an account of their own, nobody needs a Plex account but the
one who runs it, and guests at a party join a playlist with a link and their name. Every playlist is a playlist in Plex
too, so it plays in Plexamp and wherever else your Plex music goes.

> **Early days.** This is the first release. It works, and it is young: back up your data folder.

<p align="center">
  <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/editor-dark.webp"><img src="docs/screenshots/editor.webp" alt="A playlist being built: search on the left, the list in the middle, suggestions on the right" width="860"></picture>
</p>

## What it does

- **Playlists together.** A playlist belongs to whoever makes it and is shared with other members to look at or to
  build along. Every entry says who added it, changes reach everybody who has the playlist open at once, and taking a
  track out can be undone. Deleted playlists wait 30 days in the trash.
- **Fast search over the whole library.** nexmix fetches the catalogue from Plex once (then only what changed, every 15
  minutes, and the whole of it at night) and searches in the browser: no request to Plex while you type. A song stands
  there once even when the library holds it on the album, a best-of and a reissue. Filters by mood, decade and genre;
  albums and artists with all their tracks.
- **Suggestions and moods.** Suggestions that fit the last tracks of the list, from the moods, styles, genres and
  decades Plex knows, from what the household's other playlists put together and, once Plex's sonic analysis is done,
  by how the tracks sound. "Fill by mood" puts a whole list together for you to tick.
- **Listening in.** 30 seconds from a good third into a track, passed through the server: the Plex token never reaches
  a browser.
- **Plex both ways.** What changes in nexmix reaches Plex about two seconds later; what is added, renamed or taken out
  in Plex or Plexamp comes into nexmix, and the playlists Plex already has show up. When both sides change at once, a
  three-way merge keeps every track. Smart playlists of Plex are shown and never written.
- **Guests by a link or a QR code.** A guest types a name, with no account and no app, and sees only that one
  playlist. The invitation says whether guests may add, sort and listen in, how many tracks each may bring and how
  long it lasts; ending it or taking a guest out works at once.
- **Accounts the way the other nex apps do them.** Passwords, a second factor (the operator may require it for
  everybody), passkeys, sign-in through OpenID Connect providers (authentik with a one-step setup, Pocket ID, Keycloak,
  Authelia, Microsoft Entra ID), invitations for members, light and dark, German and English.
- **An interface for programs** under `/api/v1` with device tokens and read-only API keys ([docs/api.md](docs/api.md)).

Music Assistant needs nothing from nexmix: it finds the playlists through its Plex provider, like any other Plex
playlist.

<p align="center">
  <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/overview-dark.webp"><img src="docs/screenshots/overview.webp" alt="The overview of the playlists" width="560"></picture>
  <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/guest-dark.webp"><img src="docs/screenshots/guest.webp" alt="A guest's view on a phone" width="220"></picture>
</p>

## Start

```yaml
services:
  nexmix:
    image: ghcr.io/derkezorm/nexmix:latest
    container_name: nexmix
    restart: unless-stopped
    ports:
      - "127.0.0.1:8620:8000"
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp:size=256m,mode=1777
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - DAC_READ_SEARCH
      - SETUID
      - SETGID
    volumes:
      - ./data:/data
    environment:
      PUID: 1000
      PGID: 1000
      TZ: Europe/Berlin
```

```sh
docker compose up -d
```

Open `http://127.0.0.1:8620`. The first account is the operator; it needs the setup code from `docker logs nexmix`.
Then, in **Settings, Plex**, sign in at plex.tv (typing an address and a token by hand is the fallback), choose the
server and the music library, and wait for the first catalogue (a library of 50,000 tracks takes a few minutes).
A second factor is not required from the start; for a server on the internet, turn it on in **Settings, Sign-in**
("Ready for the internet?" says so).
`docker-compose.yml` in this repository builds from source and explains every option.

Locked out without a mail server: `docker exec -it -u $PUID:$PGID nexmix python -m app.reset_link <account>` makes a
password link that works once.

### Updating

```sh
docker compose pull && docker compose up -d
```

The database moves to the new version by itself at the start. When a version changes the database, a backup of the
old state is made first and kept in `data/backups/` (**Settings, Backup** restores it).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `NEXMIX_PUBLIC_URL` | from the request | The address people use; the setting in the interface wins |
| `NEXMIX_TRUSTED_PROXIES` | none | Reverse proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` count, comma separated |
| `NEXMIX_SETUP_TOKEN` | made at start | The code the first account needs |
| `NEXMIX_OPERATOR_NETWORKS` | none | Networks the operator's settings may be changed from |
| `NEXMIX_API_DOCS` | `false` | Serves the description of the API (`/api/v1/openapi.json`, `/api/v1/docs`) |
| `NEXMIX_DATA_DIR` | `/data` | Database, logs, backups, secrets; `NEXMIX_LOCALES_DIR` holds extra languages |
| `NEXMIX_SECRET_KEY` | made on first start | Protects server-side secrets |
| `NEXMIX_SESSION_DAYS` | `30` | Days a remembered session lasts without use |
| `NEXMIX_COOKIE_SECURE`, `NEXMIX_COOKIE_SUFFIX` | `auto`, none | Secure flag of the cookies; a suffix keeps two instances on one host apart |
| `NEXMIX_LOG_LEVEL` | stored setting | `quiet`, `normal`, `detailed` or `trace` |
| `NEXMIX_DECODE_SLOTS` | `1` | Pictures unpacked at the same moment |
| `NEXMIX_COVER_CACHE_MB` | `200` | Room for the album covers kept on disk |
| `NEXMIX_SERVER_SEARCH_FROM` | `150000` | Past this many tracks the interface searches on the server instead of in the browser |
| `PUID`, `PGID`, `TZ` | `1000`, `1000`, none | Owner of the data directory, time zone of the log and of the nightly sync of the catalogue |

Set by the image or used by the tests only: `NEXMIX_PORT`, `NEXMIX_FRONTEND_DIST`, `NEXMIX_UPDATE_URL`,
`NEXMIX_DISABLE_BACKGROUND`, `NEXMIX_ARGON2_TIME`, `NEXMIX_ARGON2_MEMORY_KIB`, `NEXMIX_ARGON2_PARALLELISM`,
`NEXMIX_PLEX_TV_URL`, `NEXMIX_PLEX_AUTH_URL`.

Everything else is set in the interface: Plex, the nightly sync, listening in, the defaults of invitations, sign-in
providers, mail, Web Push, backups, the log, API keys.

## Behind a reverse proxy

nexmix speaks plain HTTP on port 8000 inside the container. For anything beyond your own machine, put a reverse proxy
with https in front, set `NEXMIX_PUBLIC_URL` (or **Settings, Sign-in, Public address**) and name the proxy in
`NEXMIX_TRUSTED_PROXIES`, so that the brakes against guessing see the real sender and cookies are marked secure.
**Settings, Ready for the internet?** checks all of it.

The live updates of an open playlist and listening in are long answers: the proxy must pass them on as they come and
not cut them after a few seconds. Caddy does that as it is:

```
mix.example.com {
    reverse_proxy 127.0.0.1:8620
}
```

With nginx, turn the buffering off for the interface:

```nginx
location / {
    proxy_pass http://127.0.0.1:8620;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

## Guests from outside

A guest's link is `<public address>/j/<code>`. Phones in your Wi-Fi reach nexmix at its address in your network;
phones on mobile data need the public address with https. The invitation dialog says when the address it uses cannot
be reached from outside, and the operator sets the right one in **Settings, Sign-in**.

What a guest reaches is the page of the link, the one playlist, the search, suggestions, covers and, where the
invitation allows it, listening in: never another playlist, an account or a setting. A code has 12 characters (about
8·10^17 possibilities); wrong codes are braked per sender, and a sender who guesses never locks the guests of a party
out. Whoever has the link can slow joining down (one address gets 60 joins in ten minutes, an invitation 300): then end
the invitation and make a new one. Ending the invitation puts every guest out at once.

## Privacy

nexmix keeps its data in the data folder and nowhere else: the accounts, the playlists with who added what, the shares,
the invitations with the guests' names, and a copy of the catalogue of your Plex library (titles, artists, albums,
moods, the covers it showed). The music stays in Plex. A guest leaves a name and nothing else; the cookie of a guest
lasts as long as the invitation.

The server talks to your Plex server, to plex.tv while you connect, and, if you set them up, to a mail server, the push
services of the browsers and the sign-in providers. Once a day it asks api.github.com whether a newer release is out
(the page **About** turns that off); nothing about you goes with it. There is no telemetry, and the interface loads no
font, script or picture from another host.

## Development

```sh
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
cd ../frontend && npm ci && npm test && npm run e2e
```

The tests never reach a real Plex: `backend/tests/fakeplex` is a stand-in with a made-up library, and the pictures in
this README show it.

## License

[AGPL-3.0](LICENSE). Third-party licences: [THIRD-PARTY.md](THIRD-PARTY.md). Security reports:
[SECURITY.md](SECURITY.md).

nexmix is not affiliated with Plex Inc.; Plex and Plexamp are their trademarks.
