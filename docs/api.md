# The nexmix API

Everything the web app does goes through `/api/v1`; the web app has no routes of its own. What is under `/api/v1`
stays as it is: new fields may come, nothing is renamed or taken away. A change that would break a program goes to
`/api/v2` beside it.

- **The description**: `/api/v1/docs` lists every route on one page (no script, nothing loaded from elsewhere). The
  machine-readable description is `/api/v1/openapi.json` (OpenAPI 3). Both are off until `NEXMIX_API_DOCS` is set to
  `true`: an instance on the internet does not hand out its list of routes unasked.
- **The snapshot**: the description is kept in `backend/tests/schema/openapi.json`, and a test fails when the interface
  changes without the snapshot being renewed on purpose.

This is the interface of nexmix 0.1. Every route that comes later is added to the description and to the snapshot,
and described here.

## Ways to sign in

| Who | How | Used for |
|---|---|---|
| A browser | The session cookie, plus the header `X-Nexmix-Client` on every changing request | The web app |
| A device (phone, later app) | `Authorization: Bearer nmd_…` | Everything the account may do, except changing the way into the account |
| A program | `Authorization: Bearer nmk_…` (an API key) | Reading (scope `read`) |

A request that carries `Authorization: Bearer` is judged by that token alone; the cookie is not looked at, and a wrong
token never falls back to it. A changing request with a token needs no `X-Nexmix-Client`: a page on another site can
set neither header.

### Sign-in providers (a browser)

Sign-in through an OpenID Connect provider is a browser's way, under `/api/v1/oidc`, the same in every nex app:
`GET /providers` (the buttons of the sign-in page, open), `GET /<short name>/start[?invite=…][&next=<page>]` and
`GET /<short name>/callback` (`/callback` for the entry `oidc`). After the provider the browser holds the session
cookie, or lands on `/login?step=code` when the code of nexmix's own second factor is due. Linking, unlinking and the
operator's list (`/me`, `/<short name>/link`, `/me/address`, `/admin/…`, `/authentik/…`) answer `403 browser_only` to a
token.

### Devices

Made in the web app (**My account, Devices and apps**, or `POST /api/v1/devices` from a signed-in browser):

- The token is shown once; nexmix keeps only a checksum. It acts with the rights of its account.
- Only a signed-in browser may make, list or revoke devices, make API keys, change the password, the second factor or
  the passkeys, change or take away the mail address, or link or unlink a sign-in provider: these routes answer `403
  browser_only` to a token. The same goes for **every route of the operator** (settings, accounts, invitations, links to
  set a new password, backups, the log, the mail and push setup): the operator's side is the browser's, even for the
  operator's own token, so a lost phone is no way into the server.
- A device stops working at once when it is revoked, when its account is blocked or deleted, when "sign out
  everywhere" is used, and while the operator requires a second factor the account has not set up yet. A lock after
  wrong passwords does not stop it: the token is a secret of its own.

### API keys

Off until the operator switches them on (**Settings, API**). A key reads as its account, never more (`scope: read`). It
runs out after 30, 90 or 365 days, or never, and can be blocked for good by the operator. A request that carries an
`Origin` header is refused: a key is for programs, not web pages.

## Errors and limits

Errors look like `{"detail": {"code": "token_invalid", "message": "No valid device token."}}`. The code is stable; the
message is English.

| Answer | Code | Meaning |
|---|---|---|
| 401 | `sign_in_required` | No session. |
| 401 | `token_invalid` | A device token that does not exist, was revoked, or whose account is blocked. |
| 401 | `key_invalid` / `api_off` | An API key that does not hold, or keys are switched off. |
| 401 | `token_wrong_kind` | An API key where a device token belongs, or the other way round. It does not count as a wrong guess. |
| 403 | `browser_only` | The route needs a signed-in browser, not a token. |
| 403 | `second_factor_setup_required` | The operator requires a second factor this account has not set up. |
| 429 | `too_many_attempts` | Five wrong passwords, or five different tokens or keys nobody holds, from one address: it rests a quarter of an hour. A token or key that exists is never turned away by it, and the same wrong one again counts once. |
| 413 | `too_large` | The request is larger than the limit; `max_mb` says how large. |
| 429 | `slow_down` | More than 120 requests a minute with one key; `Retry-After` says when to go on. |
| 429 | `preview_too_often` | More than 120 previews in an hour by one listener; `Retry-After` and `retry_after` say when the next one may begin. |
| 503 | `preview_busy` | Too many previews at once on the server; try again in a moment. |
| 429 | `too_many_streams` | Too many event streams open at once for one account (or one guest); close one. |
| 409 | `too_many_playlists` | The account holds 500 playlists (`max`), not counting the trash. |
| 503 | `plex_busy`, `plex_unreachable`, `plex_timeout`, `plex_error`, `plex_refused` | A cover while Plex does not deliver, or while all places for covers are taken: `Retry-After` says when to ask again. After a failure no cover asks Plex for 30 seconds. |
| 404 | `not_found` | Not there, or not for this caller: a playlist the account may not see is `404`, never `403`. |

A wrong token counts against the sender like a wrong password (the same wrong token once).

## Routes for programs

### `GET /api/v1/me`

With an API key: who the key speaks for: `name`, `display_name`, `scope` and the nexmix `version`.

### `GET /api/v1/auth/me`

With a session or a device token: the account, its profile and what this server offers it. The profile holds `theme`
(`system`, `light`, `dark`), `timezone` and `timezone_source`, and `notify_login`.

### `GET /api/v1/health`

`{"status": "ok", "version": "…"}`, open to everybody (the container's health check uses it).

### `GET /api/v1/servers`

With a session, a device token or an API key: the media servers nexmix knows, each with `id`, `kind` (`plex`), `name`,
`active` (connected; a server let go of rests with its numbers and the copies of the playlists on it), `library`,
`ready` (its catalogue is there), `primary` (the one the Plex card shows, the source of new playlists) and
`capabilities` (`moods`, `sound`, `smart_playlists`, `duplicates`, `changes`). Never an address or a token.

Every media server has a catalogue of its own. The routes of the catalogue take `server` (an `id` of the list) and
answer for that one; without it, for the first one connected. Track, album and artist ids are nexmix's own numbers,
each the thing of one server only (a Plex server's `ratingKey`, while no other thing has that number); a playlist's
tracks are of its server (`server` of the playlist).

### `GET /api/v1/catalog/status`

With a session, a device token or an API key: where the catalogue of the Plex library stands (`server` names it). `connected` and
`library` (Plex is connected, a music library chosen), `ready` (a catalogue is there), `counts` (`artists`, `albums`,
`tracks`, `songs`, `moods`), `synced_at` and `full_at`, the run under way (`running`, with `step` and `progress` in
thousandths) or waiting (`queued`: `full` or `partial`), the `last` run that ended (`state`, `error` as a code), the
sonic analysis of Plex (`analysis.state`: `unknown`, `off`, `running` with `percent`, `on`), the time of the nightly
whole sync (`nightly`, `HH:MM` on the server's clock, empty for none), and where the interface searches (`search`:
`browser`, or `server` past `NEXMIX_SERVER_SEARCH_FROM` tracks).

### `GET /api/v1/catalog`

With a session, a device token or an API key: the whole catalogue in a compact form, packed (`gzip`) when the client
takes it, named by an `ETag` (the generation and a digest of the content); `If-None-Match` with the same name answers
`304`. `409 no_catalog` before the first sync. A JSON object of lists that point at each other by place:

- `moods`, `genres`, `styles`: names, sorted.
- `artists`: `[id, name, [moods], [styles]]`.
- `albums`: `[id, title, artist, year, cover, [genres], [styles], [moods], added]` (`artist` a place in `artists`,
  `-1` unknown; `cover` 1 when there is one; `added` Plex's time).
- `tracks`: `[id, title, album, own artist, seconds, listeners, plays, [moods], disc, number, song, original year]`
  (`own artist` empty where it is the album's; `song` the id of the version shown for the song, so the track is that
  version when `song` equals its `id`; `original year` the earliest album year of the song, 0 for none).

### `GET /api/v1/catalog/search`

With a session, a device token or an API key: `q` (words in any order, parts of words, without accents), `kind`
(`tracks`, `albums`, `artists`), and for tracks the filters `mood` (a mood by its name, or `g:<group>`), `decade`
(of the original year, such as `1990`), `genre` (of the album), `limit` (up to 200). Tracks come one per song (the
version shown, or the one found when only it matches), each with `song` and `versions` (how many the library has);
`total` counts the songs found, `took_ms` the time. The same rules as the search in the browser.

`GET /api/v1/catalog/facets` lists the moods (with their tracks), the groups of moods, the genres and the decades;
`GET /api/v1/catalog/albums/{id}` an album with its tracks in order; `GET /api/v1/catalog/artists/{id}` an artist with
its best-known songs and its albums.

### `/api/v1/playlists`

`GET` lists the playlists the account sees (a member: their own and the ones shared with them; the operator: all), each
with `access` (`owner`, `operator`, `edit`, `read`), `owner`, `shares`, `version`, `count`, `duration` (seconds), the
album ids of up to four `covers`, the first `people` who added something; `?trash=true` the deleted ones the account may
bring back, with `purge_at` (30 days after `deleted_at`), and `server`, the media server the playlist takes its tracks
from (its catalogue is the one to search; `null` before any was connected). `GET /{id}` adds `items` in order (each entry has its own
`id`, the `track`, title, artist, album, `album_id`, `year`, `duration`, `song`, `missing` when the track is no longer in
the catalogue, `from_plex` when it was put in through Plex, `plex_hint` when it is in nexmix only (`refused`: Plex
did not take its track, five tries over some 80 minutes; `elsewhere`: the connected server has no such track),
`added_by`) and the playlist's `moods`. Each playlist has
`smart` and `plex`: how its copy in Plex stands (`state`: `synced`, `pending`, `waiting` when Plex was not reached and
it is tried again, `error` with its code in `error`, `off` without Plex), `total` of a smart playlist, `gone_in_plex`
for one deleted there, and `note` when it was sorted in Plex and here at the same time. These read with an API key too;
everything else needs a session or a device token. A playlist the account may not see answers `404`.

- `POST` `{"name"}` makes one; `PUT /{id}` `{"name"}` renames it; `DELETE /{id}` moves it to the trash,
  `DELETE /{id}?purge=true` deletes it from there for good; `POST /{id}/restore` brings it back. Deleting, bringing
  back and sharing: the owner and the operator only.
- `POST /{id}/items` `{"tracks": [...]}` adds at the end; a track already in the playlist (or not in the catalogue) is
  skipped and named in `skipped`. `POST /{id}/items/remove` `{"entries": [...]}` takes entries out;
  `POST /{id}/items/restore` `{"entries": [...], "after": entry|null}` brings them back (with who added them);
  `POST /{id}/items/move` `{"entry", "after": entry|null}` moves one (`null`: to the start). Each answer, and the event
  every browser with the playlist open gets, carries the new `version`, the `entries` added or brought back, the whole
  `order` of entry ids and a `summary`. An operation that finds nothing to do (a track already there, an entry gone
  meanwhile) is no error: `op` is `none` and the version stays.
- `GET /{id}/shares` lists the shares and the members to share with; `PUT /{id}/shares/{account}` `{"access": "read"|
  "edit"}`, `DELETE /{id}/shares/{account}`.
- Plex: every playlist is a playlist in the connected Plex account, changes go both ways (a change here reaches Plex
  about two seconds later; entries added, taken out or a name changed in Plex arrive within 15 seconds, entries only
  moved there within two minutes, with more than 80 playlists within `order_minutes` of `GET /api/v1/plex/playlists`,
  within 15 seconds while the playlist is open). `POST /{id}/sync` syncs it now (also after a failure, and offers a
  track Plex refused again at once); it needs the right to change the playlist (`403` `read_only` otherwise).
  `POST /{id}/plex-order` takes the order Plex had when both sides sorted at the same time (the note's undo),
  `DELETE /{id}/plex-order` keeps this one. A smart playlist of Plex is only read: changing it answers `409`
  `smart_playlist`, and it takes no guests. Entries added by guests go to Plex like all others (their names stay in
  nexmix); a playlist deleted in Plex goes to the trash, and its invitation for guests ends.
- `GET /{id}/events`: Server-Sent Events (`hello` with the version, `items` (also with `op` `sync` for what came from
  Plex), `playlist` for a rename or the trash, `plex` when its sync with Plex changes state, `access` when the shares
  change, `gone` when the account may no longer see it, or the session, device or key that opened the stream is
  signed out, revoked or blocked: looked at every 20 seconds). A version that is not the next one means an event was missed:
  read the playlist again. `guests` says how many guests a running invitation has now.
- Cards and the playlist carry `guests`: the guests of a running invitation, `null` without one.
- Hiding: `PUT /{id}/hidden` hides a playlist from the own overview, `DELETE /{id}/hidden` shows it again (whoever may
  see it; twice is once). It is a view of this account alone: nothing goes to Plex, no right changes, nobody else sees
  a difference. Cards and the playlist carry `hidden`; the list carries `show_smart_playlists`, the account's choice
  (`PUT /api/v1/me/preferences`, off from the start) whether its overview shows the smart playlists of Plex. The list
  itself always has every playlist the account sees.

### Guests: `/api/v1/playlists/{id}/invite`, `/api/v1/join/{code}`, `/api/v1/guest`

People without an account join one playlist by a link (`<public address>/j/<code>`) or its QR code and a name. Who may
change the playlist (owner, `edit`, operator; a session or a device, never an API key) invites:

- `GET /{id}/invite`: the invitation that has not ended (`invite`: `code`, `link`, `qr` as SVG, `state` `waiting` or
  `running`, `may_add`, `may_sort`, `max` tracks per guest, `hours`, `preview`, `expires_at`), the `guests` with their
  `count` of entries, the `defaults` and the `address` the link starts with (`reach`: `internet`, `home` or `machine`).
- `POST /{id}/invite` makes one that waits (or answers the one there is); `PUT /{id}/invite` changes the rights and with
  `"start": true` starts it (it lasts `hours` from its start); `DELETE /{id}/invite` ends it: every guest is out at
  once. `DELETE /{id}/invite/guests/{guest}` takes one guest out; what it added stays.

Open to everybody with the code. A wrong code counts against its sender (after 30 within ten minutes, every wrong code
of the sender is `429` with `Retry-After`); a wrong code never holds up a right one, which counts against its invitation
(the page, its covers and new guests each on their own). New guests also count per invitation and sender: 60 within ten
minutes from one address, 300 for the invitation. `GET /api/v1/join/{code}` (the playlist's name, count, length, covers, who invites, whether this browser
joined), `POST /api/v1/join/{code}` `{"name"}` (1 to 24 visible characters, no control or invisible ones, no mix of Latin,
Cyrillic and Greek letters; a name taken in another case or width gets a number) gives the browser the guest cookie
(HttpOnly, SameSite=Lax, Secure as the session cookie, only for `/api/v1`, until the invitation ends; `GET
/api/v1/guest` renews it when the invitation lasts longer), and
`GET /api/v1/join/{code}/cover/{album}` the covers of the playlist's mosaic.

With the guest cookie only, and nothing else: `GET /api/v1/guest` (the playlist with the names at its entries and the
guest's own marked `mine`, the rights, the guest's `count`), `POST /api/v1/guest/items` `{"tracks"}` (within `max`,
all or none), `/items/remove` (its own, or with `may_sort` any), `/items/restore`, `/items/move` (`may_sort`),
`POST /api/v1/guest/leave`, `GET /api/v1/guest/catalog` (the compact catalogue, `ETag`), `/catalog/search`,
`/suggestions` (the songs that fit the last five tracks of the playlist, by the rules of `/api/v1/suggestions` in the
kind `fit`, without the playlist's songs and without any voice of other playlists; the browser works them out itself
from the compact catalogue), `/cover/{album}`, and `GET /api/v1/guest/events` (Server-Sent Events with the version
only; `gone` when the guest is out). A guest whose invitation allows it (`preview`) listens in through
`/api/v1/preview` with the guest cookie, with limits of its own.
Without the cookie `401 guest_required`; taken out `403 guest_removed`; ended or run out `403 invite_ended`, also as
soon as the person who invited may no longer change the playlist. People of the household appear to guests by their
display name only (`inviter`, `by` empty without one). Bringing an entry back needs `may_add`.

### `GET /api/v1/preview/{track_id}`

With a session or a device token, or the cookie of a guest whose invitation allows listening in (`403 preview_off`
when it does not; a request with an account's credentials listens as the account): about 30 seconds of a track of the
catalogue as MP3 (192 kbit/s), from 35 % of its length, converted by Plex and passed through the server (the Plex
token never leaves it); at most about 1 MB. One preview at a time per listener (an account or a guest; a new one ends
the one before), 120 an hour (`429 preview_too_often`), a few at once on the server (`503 preview_busy`).
`403 preview_off` while the operator has switched listening in off (setting `preview_allowed`), `404` for anything
that is not a track of the catalogue. `GET /api/v1/preview` says whether it is on (`allowed`), how long a preview
plays (`seconds`), and how many are `left` this hour (`retry_after` when none).

When Plex refuses with `400` or a `5xx`, or answers without sound, the server asks once more after half a second with a
new session (the same preview for the hour). After that, or at once for a refused token or a missing track, the
answer is `502 plex_refused`, `502 plex_error`, `503 plex_busy`, `502 plex_unreachable`, `504 plex_timeout` or `404`;
the server's log has one line per refusal with Plex's status and the start of its answer.

Without `Range` (or with `bytes=0-`) the sound comes as Plex sends it, without a length. A request for a part
(`Range: bytes=0-1`, as Safari sends first, or any part further in) gets `206` with `Content-Range` and
`Content-Length`: the server reads the preview whole first (up to about 1 MB). A whole preview is kept for three
minutes for the same listener, and the next requests with `Range` for that track are answered from it without counting
as a new preview; a part beyond the end is `416`. A request without `Range` is always a new preview.

### `/api/v1/suggestions`

With a session, a device token or an API key. Songs are named by the id of the version shown for them (`song` in the
compact catalogue); each answer track is that version, with its `reason` (`moods`: the two strongest it shares with the
seed, `style`, `decade`, `together`: playlists it shares with the seed, `sound`: whether it sounds alike).

- `GET ?seed=<track>&seed=…&mode=fit|known|new&playlist=<id>&limit=` songs that fit the seed tracks (at most two per
  artist, one for `new`; never the seed's songs, the playlist's, or the hidden ones). With `playlist` (one the account
  may see) its songs stay out, without `seed` its last five tracks are the seed, and the songs come from its server's
  catalogue (else from `server`'s, else from the first one connected; the same holds for `/mood`, `together` and
  `sonic` take `server`).
- `GET /mood?moods=<name>&moods=…&decades=1980&minutes=60&hits=55&playlist=<id>&seed=<n>` fills by mood: songs of the
  chosen moods for about `minutes`, `hits` from 0 (discoveries) to 100 (hits), at most two per artist, the same artist
  never twice in a row; the same `seed` gives the same mix.
- `GET /together?songs=<song>&songs=…` the songs that stand in the same playlists as these, with how many (`count`):
  only playlists the account may see count (`playlists`: how many held one of the songs).
- `GET /sonic?track=<track>&track=…` (up to five) the songs that sound closest by Plex's sonic analysis (`distance`,
  smaller is closer); empty while the analysis is `off` or the tracks are not analysed yet (`state` as in the
  catalogue's status).
- `GET /hidden` the songs the account asked not to be suggested again; `POST /hidden` `{"track"}` adds the song of a
  track (every version of it), `DELETE /hidden/{track}` takes it back. These two need a session or a device token.

The browser works the suggestions and the filling by mood out itself from the compact catalogue, by the same rules,
and asks only `together`, `sonic` and `hidden`; past `NEXMIX_SERVER_SEARCH_FROM` tracks it asks the server instead.

### `GET /api/v1/catalog/cover/{album_id}?s=80|200|400`

With a session, a device token or an API key: the cover of an album of the catalogue, JPEG or PNG, square, fetched
through the server (the Plex token never leaves it) and kept on its disk. `ETag` and `If-None-Match` work; the answer
may be kept for a week. A few covers are fetched from Plex at once; one that finds no place within a second, or asks
while Plex is failing, is `503` with `Retry-After` (the rest of the server never waits for Plex's covers).

## Routes of the operator's browser

The Plex connection lives under `/api/v1/plex` (`GET` the state; `POST /pin` and `GET /pin/{id}` sign in through
plex.tv, `DELETE /pin/{id}` gives such a sign-in up; `POST /connect` with the PIN and a server, or with `url` and
`token`; `GET /libraries`, `PUT /library`; `DELETE` disconnects and stops a sync under way; `GET /playlists` tells
how the playlists stand in Plex, `POST /playlists/sync` syncs them all now and looks for new ones there), and
`POST /api/v1/catalog/sync` fetches the whole catalogue again (a run already waiting or running is not doubled:
`{"started": false}`). Only the operator, only in a signed-in browser. A sign-in through plex.tv belongs to the
session that began it: for every other one, even of another operator, it does not exist (`pin_expired`). No answer
ever holds a token.

## The account, signing in and the setup (a browser, mostly)

- `GET /api/v1/setup`, `POST /api/v1/setup` `{"name", "password", "language", "code"}`: the first account, with the
  setup code from the log; only while nexmix has no account.
- `GET /api/v1/auth/methods` (what the sign-in page offers), `POST /auth/login` `{"name", "password", "remember"}`,
  `POST /auth/login/totp` `{"code"}` and `/login/totp/cancel` (the second step), `POST /auth/passkey/begin` and
  `POST /auth/passkey` (sign in with a passkey), `POST /auth/logout`, `POST /auth/logout-all`, `POST /auth/forgot`
  `{"name"}` (a link by mail, the same answer whether the account exists or not).
- `GET /auth/sessions`, `DELETE /auth/sessions/{uid}`; `PUT /auth/password`; `POST /auth/totp/begin`, `/confirm`,
  `/disable`, `/recovery` (the second factor and new recovery codes); `GET /auth/passkeys`, `POST /auth/passkeys/begin`,
  `POST /auth/passkeys`, `POST /auth/passkeys/{uid}/remove`; `PUT` and `DELETE /auth/avatar`, `GET
  /api/v1/avatars/{account}`; `POST /auth/setup/done` and `/setup/codes` (the steps after the first sign-in).
- `/api/v1/me`: `PUT /profile` (display name, theme, time zone, sign-in notice), `PUT /language`, `PUT /preferences`
  (also `show_smart_playlists`),
  `PUT /email` and `DELETE /email/pending` (a new mail address, confirmed by a link: `GET` and `POST
  /api/v1/email/confirm/{token}`), `POST /whats-new/seen`.
- Invitations of members: `GET /api/v1/invite/{token}` and `POST /api/v1/invite/{token}` `{"name", "password"}` (open,
  with the token of the link); a link to set a new password: `GET` and `POST /api/v1/reset/{token}`.
- `/api/v1/devices` (`GET`, `POST {"name"}`, `DELETE /{uid}`) and `/api/v1/api-keys` (`GET`, `POST {"name", "days", "scope"}`,
  `DELETE /{id}`): only from a signed-in browser. `/api/v1/push` registers the browser or device for Web Push (`GET`,
  `POST /devices`, `POST /devices/lookup`, `PUT` and `DELETE /devices/{id}`, `POST /test`).
- `GET /api/v1/about` (version, licence, where the source is) and `GET /api/v1/about/updates` (whether a newer release
  is out); `PUT /about/updates` `{"update_check"}` and `POST /about/updates/check` for the operator.
- `GET /api/v1/locales`, `GET /api/v1/locales/{code}` (open: the languages and their texts); `PUT` and `DELETE
  /api/v1/locales/{code}` upload or remove a language (the operator).

## The operator's side (a browser only)

Every route here answers `403 operator_only` to a member and `403 browser_only` to a token, even the operator's own. Where
`NEXMIX_OPERATOR_NETWORKS` is set, a request from elsewhere is `403 operator_network`.

- `GET` and `PUT /api/v1/settings` (sign-in, public address, second factor for everybody, listening in
  (`preview_allowed`), the defaults of invitations for guests (`guest_preview`, `guest_max`, `guest_hours`), the
  nightly sync (`catalog_full_at`), mail, backups, API keys); `GET /settings/readiness` ("Ready for the internet?"),
  `POST /settings/mail-test` `{"to"}`, `GET` and `PUT /settings/push`, `POST /settings/push/renew`.
- Accounts: `GET /api/v1/accounts`, `PUT /{id}/role`, `POST /{id}/block` and `/unblock`, `POST /{id}/sign-out`, `POST
  /{id}/totp/reset`, `POST /{id}/reset-link`, `DELETE /{id}`. Invitations of members: `GET`, `POST {"days", "email", "send"}` and
  `DELETE /{id}` under `/api/v1/invites`. API keys of everybody: `GET /api/v1/admin/api-keys`, `POST /{id}/block`.
- Sign-in providers under `/api/v1/oidc/admin` (`providers` with `POST`, `PUT /{id}`, `DELETE /{id}`, `PUT /order`,
  `GET /{id}/impact`; `links`; `DELETE /accounts/{account}/links/{provider}`) and authentik in one step
  (`/api/v1/oidc/authentik/address`, `/setup`, `/blueprint`). A provider's own routes for a person: `GET /oidc/me`,
  `POST` and `DELETE /oidc/{slug}/link`, `POST /oidc/me/address`.
- Backups: `GET /api/v1/backups`, `POST` (make one), `POST /upload`, `POST /{name}/check` (the trial run), `POST
  /{name}/download`, `POST /{name}/restore` and `DELETE /{name}` (the last three with `{"password"}` again). A restore
  happens at the next start; the server ends itself after staging it.
- The log: `GET /api/v1/logs` (`level`, `search`, `limit`), `GET /logs/download`, `DELETE /logs`, `GET` and `PUT
  /logs/level`. `GET /api/v1/storage`: the data folder, its size and file system.
- Plex and the catalogue: see "Routes of the operator's browser" above.

Outside `/api/v1` the server answers only the files of the built interface and `/manifest.webmanifest`; nothing there
changes anything.
