# The nexmix API

Everything the web app does goes through `/api/v1`; the web app has no routes of its own. What is under `/api/v1`
stays as it is: new fields may come, nothing is renamed or taken away. A change that would break a program goes to
`/api/v2` beside it.

- **The description**: `/api/v1/docs` lists every route on one page (no script, nothing loaded from elsewhere). The
  machine-readable description is `/api/v1/openapi.json` (OpenAPI 3). Both are off until `NEXMIX_API_DOCS` is set to
  `true`: an instance on the internet does not hand out its list of routes unasked.
- **The snapshot**: the description is kept in `backend/tests/schema/openapi.json`, and a test fails when the interface
  changes without the snapshot being renewed on purpose.

This is the interface of nexmix 0.2. Every route that comes later is added to the description and to the snapshot,
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

With a session, a device token or an API key: the media servers nexmix knows, each with `id`, `kind` (`plex`,
`jellyfin` or `music_assistant`), `name`, `active` (connected; a server let go of rests with its numbers and the copies of the playlists on
it), `library`, `ready` (its catalogue is there), `primary` (the first one connected, the source of a new playlist
unless another is named) and `capabilities` (`moods`, `sound`, `smart_playlists`, `duplicates`, `changes`, and
`playlists`: nexmix keeps its playlists there; Jellyfin and Music Assistant have no moods and no sonic analysis;
`changes` is `events` for Music Assistant, which tells of every change at once). Never an address, a token or an id of
the server.

`DELETE /api/v1/servers/{id}` (the operator, in a browser) lets go of a server: its token and its catalogue go, the
server itself stays as it is, and its mirrors are forgotten (the playlists there stay as they are, nexmix no longer
keeps them in step). While a playlist (not in the trash) takes its tracks from it the answer is `409
server_is_source` with `count`: move them first, for instance with "take all over" and `disconnect` (below).

Every media server has a catalogue of its own. The routes of the catalogue take `server` (an `id` of the list) and
answer for that one; without it, for the first one connected. Track, album and artist ids are nexmix's own numbers,
each the thing of one server only (a Plex server's `ratingKey`, while no other thing has that number); a playlist's
tracks are of its server (`server` of the playlist).

### `GET /api/v1/catalog/status`

With a session, a device token or an API key: where the catalogue of a media server stands (`server` names it; without it the first one connected; `kind` is its kind). `connected` and
`library` (the server is connected, a music library chosen), `ready` (a catalogue is there), `counts` (`artists`, `albums`,
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
  `-1` unknown; `cover` 1 when there is one; `added` the server's time).
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
the catalogue, `from_plex` when it was put in through its server (Plex, Jellyfin or Music Assistant), `plex_hint` when it is in nexmix
only (`refused`: the server did not take its track, five tries over some 80 minutes; `elsewhere`: the connected server
has no such track; `missing`: the server lost it while its track left the library, it goes back after the next run of
the catalogue),
`added_by`) and the playlist's `moods`. Each playlist has
`smart` and `plex`: how its copy on its server stands (`kind`: `plex`, `jellyfin` or `music_assistant`; `state`: `synced`, `pending`,
`waiting` when the server was not reached and it is tried again, `error` with its code in `error`, `off` without a
server), `total` of a smart playlist, `gone_in_plex` for one deleted there, `note` when it was sorted there and here at
the same time, and `loss` when the server lost many entries at once (see below). These read with an API key too;
everything else needs a session or a device token. A playlist the account may not see answers `404`.

- `POST` `{"name", "server"}` makes one, taking its tracks from `server` (an `id` of `GET /api/v1/servers`; without
  it the first one connected; `422 invalid_server` for one that is not connected); `PUT /{id}` `{"name"}` renames it; `DELETE /{id}` moves it to the trash,
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
  `smart_playlist`, and it takes no guests. On another Plex server than its own it is never made as an ordinary
  playlist: its sync stands at `error` `smart_not_here` until it is back on its own server. Entries added by guests go to Plex like all others (their names stay in
  nexmix); a playlist deleted in Plex goes to the trash, and its invitation for guests ends.
- Deleted on the server: a playlist goes to the trash only when the server's list, read again, lacks it, no other of
  nexmix's playlists is missing from that list at once, and the server says of the playlist itself that it is gone
  (Plex: its entries answer `404`; Jellyfin: its entries asked without a user answer `404`). A `404` to everything
  (a proxy, a changed base path) is `error` `playlists_not_found`; several playlists missing at once too, and
  `missing` of `GET /api/v1/plex/playlists` or `GET /api/v1/jellyfin/{id}/playlists` says how many (nothing goes to
  the trash, the list is looked at on); a playlist the list lacks but the server still has is `error`
  `playlist_unreadable`; the Jellyfin user of the playlists gone is `error` `jellyfin_user_gone` (connect again). None
  of these ends an invitation. A copy deleted on the server keeps its key: the server's list never brings it in as a
  new playlist, and `restore` takes it up again where the server has it after all, else makes it anew.
- Jellyfin: a playlist whose source is a Jellyfin server is a playlist there, of the Jellyfin user chosen when
  connecting and public, kept both ways like one of Plex (changes there arrive within 15 seconds, moves too: Jellyfin's
  list carries a mark of every change; made, renamed or deleted there also through Jellyfin's websocket). Playlists of
  that user made in Jellyfin come to nexmix and belong to the operator, but only when nexmix is connected as that user
  (Quick Connect by him): with an API key nexmix cannot tell whose a playlist is, and takes none in. A track standing
  twice in Jellyfin is one entry here. The playlist's `plex` carries `kind` (`plex` or `jellyfin`); while Jellyfin
  starts or cannot be reached it is `waiting` (`jellyfin_starting`, `jellyfin_unreachable`, `jellyfin_timeout`), a
  token Jellyfin no longer takes is `error` `jellyfin_refused` (connect again), a change this user may not make there
  `error` `jellyfin_forbidden`.
- Music Assistant: a playlist whose source is a Music Assistant is one of its own playlists there (provider `builtin`;
  its dynamic and read-only playlists are never taken in). Every one of them made in Music Assistant comes to nexmix and
  belongs to the operator (Music Assistant keeps no owner). Changes made there arrive at once by its events (the list
  is looked at every minute besides, and every playlist is read again after its websocket was closed). Music Assistant
  can neither move an entry nor rename a playlist, and takes entries out by their place only: nexmix takes out by the
  places of a reading of the moment and reads again afterwards; when the playlist changed there meanwhile, whatever was
  taken by mistake is put back and the sync merges again. A new order takes out everything from the first entry out of
  place and puts it back in order; a new name makes a new playlist under it, copies every entry over and deletes the
  old one (its id changes; neither is ever taken in as another playlist). A name another playlist there has already is
  made unique there (`Sommer (2)`) and stays nexmix's own here. A track Music Assistant passes over in that copy stays
  here with `plex_hint` `missing` (an entry whose file Music Assistant no longer knows stays behind with the old one).
  Tracks Music Assistant does not take (unknown, or there already) are offered again later as with Plex. The
  playlist's `plex` carries `kind` `music_assistant`; while Music Assistant restarts or cannot be reached it is
  `waiting` (`ma_unreachable`, `ma_timeout`, `ma_starting`), and while a task of nexmix's there has not been seen to end
  (cancelling it was refused, the websocket broke) `waiting` `ma_task_open`: nothing is written to the playlist and
  nothing taken from it until Music Assistant says it ended, and what it took then that was not meant goes back. A
  token it no longer takes (run out after a year, or revoked) is `error` `ma_refused` (connect again), one that is no
  admin's any more `error` `ma_not_admin`, a playlist that kept changing there while nexmix wrote it `error`
  `playlist_changed_meanwhile` (tried again later). Nothing goes out by place unless the playlist read right before is
  the one merged and nobody else's task at it is under way; when somebody adds there right while nexmix takes out by
  place all the same, Music Assistant may have taken that track instead (it names no entry, so which one is not known):
  `plex.concurrent` says `{"at", "count"}` until `DELETE /{id}/concurrent` (seen; needs the right to change the
  playlist).
- Entries the server lost: before an entry taken out on the server is taken out here, the server is asked whether its
  track is still in its library (one question per sync, Jellyfin 200 tracks, Plex 50 to a request; when the question
  fails nothing is taken, and the sync is tried again). When its track left the library (Jellyfin drops an entry
  without a word when the file is renamed or moved, or the library is removed), the entry stays here with `plex_hint`
  `missing`, whatever the size of the playlist, and goes back to the server after the next run of the catalogue (under
  the track's new id where the file was renamed). When the server's playlist loses many entries at once (five or more)
  while some of their tracks left the library, nothing of it is taken: the entries stay with the same hint and
  `plex.loss` says `{"at", "count"}`. A playlist emptied on the server while all its tracks are still there was emptied
  on purpose and is taken. `POST /{id}/missing/remove` takes them out here too
  (needs the right to change the playlist; the answer is the change, as for `items/remove`, and the entries can be
  brought back with `items/restore`), `DELETE /{id}/missing` keeps them (the note goes; they go back to the server
  after the next run of the catalogue). The same holds for Plex.
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

### Mirrors, copying and moving: `/api/v1/playlists/{id}/mirrors`, `/api/v1/playlists/{id}/move`

A playlist takes its tracks from one server (its source, `server`) and may be **mirrored** on others: a playlist
there with the tracks of that server that stand for its entries, kept in step both ways like the source, each server
with its own common state (no change goes back and forth). Entries belong to the source; on every mirror each entry
has the track that stands for it there, found by `match` (`how`: `mbid`, `album`, `length`, `renamed`, all sure;
`song` and `title` are only offered in a preview), chosen by hand (`hand`), left out on purpose (`skip`) or not found
(`none`). An entry without a track there is not in the mirror and never counts as taken out there; its `absent` lists
the servers of such mirrors. A track put into a mirror on its server is looked for in the source's library: found by
a sure step it becomes an entry (`added_by_name` the server's kind), else it is listed at the mirror as `extras`
("only in <server>"), neither taken over nor taken out until somebody decides. A mirror deleted on its server says
`state` `gone`; the playlist stays. A mirror of a playlist in the trash rests until it is brought back; deleted for
good, the playlist takes its mirrors from their servers too. Cards and the playlist carry `mirrors`.

- `GET /{id}/mirrors` (whoever sees the playlist): `{"mirrors": [{"server", "kind", "name", "state" (`synced`,
  `pending`, `waiting`, `error`, `gone`, `off`), "error", "synced_at", "absent" (entries without a track there),
  "extras": [{"id", "title", "artist", "album", "duration"}], "emptied" (`{"at", "count"}` when the mirror was
  emptied on its server and waits for an answer, else null)}]}`.
- `POST /{id}/mirrors/preview` `{"server", "choices", "move"}` (the owner and the operator): what copying or moving to
  the server would find, nothing written: `entries`, `found`, `unsure`, `missing`, `preselect`, and `rows` for every entry not found or
  found by an unsure step (by its `track`, the same track of several entries once, with `count`): `title`, `artist`,
  `album`, `duration`, `how`, `found` (the track of the target, `{"track", "title", "artist", "album", "duration"}`,
  or null), at most 2,000 (`more` says how many beyond). `choices` maps an entry's track to a track of the target
  (`hand`) or null (`skip`); a track of another server is not taken. Search the target's catalogue for a choice with
  `GET /api/v1/catalog/search?server=`. Copying takes an entry found by an unsure step (`preselect` true, it counts as
  found); moving (`"move": true`) and taking all over only offer it (`preselect` false): it counts as found once its
  track is chosen, else the entry keeps its own track and says it is not on the new source.
- `POST /{id}/mirrors` `{"server", "choices", "once"}` (the owner and the operator, `201`): a mirror on the server,
  synced at once (`409 already_mirrored`, `same_server`, `smart_stays`: a smart playlist stays on its own server,
  `in_trash`, `server_not_ready`). With `"once": true` instead a new playlist with the server as its source and the
  entries found there, in order, each still saying who put it in, no tie to this one; `playlist` names it.
- `POST /{id}/move` `{"server", "choices", "old"}` (the owner and the operator): the server becomes the source. Its
  entries take the server's tracks; an entry without one keeps its own and says `plex_hint` `elsewhere` (it stays on
  the server before). The copy on the server before stays a mirror (`old` `keep`, the default) or is deleted there
  (`delete`). A mirror already there becomes the source with its state and its choices. Guests, shares, the trash and
  the suggestions follow the new source. The answer has `found` of `entries`.
- `DELETE /{id}/mirrors/{server}` (the owner and the operator): lets go of a mirror; the playlist there stays as it
  is. `POST /{id}/mirrors/{server}/remake`: a mirror deleted on its server is made there anew.
- `PUT /{id}/mirrors/{server}/entries/{entry}` `{"track"}` (whoever may change the playlist): the track of the
  mirror's server that stands for the entry (`409 track_taken` when another entry has it there), null to leave the
  entry out of that mirror.
- `POST /{id}/mirrors/{server}/extras/{extra}/take` `{"track"}`: a track only in the mirror taken over as a track of
  the source, a new entry at the end standing for the mirror's track (the answer is the change, as for `items`).
  `DELETE /{id}/mirrors/{server}/extras/{extra}`: dropped; it is taken out of the mirror at its next sync.
- `POST /{id}/mirrors/{server}/emptied` `{"empty"}` (whoever may change the playlist): the answer to a mirror emptied
  on its server, which nexmix never takes over by itself. `true` empties the playlist everywhere (in nexmix, on the
  source and every mirror), `false` keeps it and fills the mirror again (`404` when nothing waits for an answer). The
  sync summary of a server (`GET /api/v1/plex/playlists`, `GET /api/v1/jellyfin/{id}/playlists`,
  `GET /api/v1/music-assistant/{id}/playlists`) counts the mirrors
  there that wait (`emptied`).

**Take all over** (the operator, in a browser): `POST /api/v1/servers/{id}/transfer/preview` `{"mode", "source",
"choices"}` (`202`) begins the same preview over every playlist of the other servers (`source`: of that one) not in the
trash, in the background (over a large library it takes seconds; the same one asked again while it runs is not begun
twice). `GET /api/v1/servers/{id}/transfer/preview` says how it stands: `{"preview": {"state" (`running`, `done`,
`stopped`, `failed`), "done", "total" (playlists), "error", "result"}}`, null when none was asked for; `result` (once
`done`) is the preview as above with `playlists` (how many) and `smart` (smart playlists stay where they are); `mode`
`mirror` leaves out the ones mirrored there already. Choices by hand need no new preview: a row chosen a track counts
as found and sure, one left out as not found. `POST /api/v1/servers/{id}/transfer` `{"mode", "old", "choices", "source", "disconnect"}`
(`202`) starts it in the background, one playlist after another between the syncs; `disconnect` (with `mode` `move`)
lets go of that server once none of its playlists is left on it. `GET /api/v1/servers/{id}/transfer` says how it
stands (`state` `running`, `done`, `stopped`, `failed`; `total`, `done`, `skipped`; `error` `still_source` when the
server could not be let go of), `DELETE` stops it after the playlist under way. One runs at a time (`409
transfer_running`); after a restart it goes on where it was.

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
catalogue as MP3 (192 kbit/s), from 35 % of its length, converted by its media server (Plex's converter, Jellyfin's
`stream.mp3`) and passed through the server (no token, no address and no id of the server ever leaves it; Jellyfin
serves sound to whoever knows an id); at most about 1 MB. A track of Music Assistant: when another connected server has
the same track (title, artist, length), its preview as above; else Music Assistant's own, the first 30 seconds as AAC
(`audio/aac`), fetched with a token good for a minute that never leaves the server. One preview at a time per listener (an account or a guest; a new one ends
the one before), 120 an hour (`429 preview_too_often`), a few at once on the server (`503 preview_busy`).
`403 preview_off` while the operator has switched listening in off (setting `preview_allowed`), `404` for anything
that is not a track of the catalogue. `GET /api/v1/preview` says whether it is on (`allowed`), how long a preview
plays (`seconds`), and how many are `left` this hour (`retry_after` when none).

When Plex refuses with `400` or a `5xx`, or answers without sound, the server asks once more after half a second with a
new session (the same preview for the hour). After that, or at once for a refused token or a missing track, the
answer is `502 plex_refused`, `502 plex_error`, `503 plex_busy`, `502 plex_unreachable`, `504 plex_timeout` or `404`
(for a track of Jellyfin `jellyfin_refused`, `jellyfin_error`, `jellyfin_starting`, `jellyfin_unreachable`,
`jellyfin_timeout`; of Music Assistant `ma_unreachable`, `ma_timeout`, `ma_refused`, `ma_error`); the server's log has one line per refusal with the server's status and the start of its answer.

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
through the server from the album's media server (no token, no address and no id of the server leaves it) and kept on
its disk. `ETag` and `If-None-Match` work; the answer may be kept for a week. A few covers are fetched at once; one
that finds no place within a second, or asks while its server is failing, is `503` with `Retry-After` (the rest of
the server never waits for covers, and a server in trouble holds up no other server's). Music Assistant's pictures
come at its own sizes (80, 256, 512 for 80, 200, 400), always as JPEG, only by a picture id of the catalogue.

## Routes of the operator's browser

The Plex connection lives under `/api/v1/plex` (`GET` the state; `POST /pin` and `GET /pin/{id}` sign in through
plex.tv, `DELETE /pin/{id}` gives such a sign-in up; `POST /connect` with the PIN and a server, or with `url` and
`token`; `GET /libraries`, `PUT /library`; `DELETE` disconnects and stops a sync under way; `GET /playlists` tells
how the playlists stand in Plex (`playlists` in all, `sources` with Plex as their source, `mirrors` of playlists of
another server, and how many of them are `synced`, `waiting`, `error`; the same for Jellyfin and Music Assistant),
`POST /playlists/sync` syncs them all now and looks for new ones there), and
`POST /api/v1/catalog/sync` fetches the whole catalogue again (a run already waiting or running is not doubled:
`{"started": false}`). Another Plex server beside the first one: `POST /plex/connect` with `"alongside": true`
(nothing moves to it; `added` names it), its library with `GET /plex/libraries?server=` and `PUT /plex/library?server=`.

Jellyfin lives under `/api/v1/jellyfin`: `GET` the connected Jellyfin servers; `POST /quick` `{"url"}` starts Quick
Connect at the server and answers the six-digit `code` to enter in Jellyfin (Quick Connect in the profile), its `id`
and `seconds` left (ten minutes); `GET /quick/{id}` says whether it was confirmed (`done`), `DELETE /quick/{id}` gives
it up; `POST /connect` `{"quick"}` redeems the confirmed code once, or `{"url", "key"}` connects with an API key of
Jellyfin. The address is checked first (`/System/Info/Public`; `422 not_jellyfin`, `bad_url`, `address_refused`),
`409 quick_off` when Quick Connect is switched off there, `404 quick_expired` after ten minutes, `503
jellyfin_starting` while Jellyfin starts. The answer has the `server`, its music `libraries` and `users`, each named by
a `handle` of nexmix's own (never an id of Jellyfin); a single library or user is chosen at once. `GET /{id}`, `GET
/{id}/choices`, `PUT /{id}/library` `{"handle"}` (the catalogue is fetched anew), `PUT /{id}/user` `{"handle"}` (the
Jellyfin user the playlists will belong to there; their play counts say how well known a track is), `DELETE /{id}`
lets go of it. The catalogue is fetched once the library and the user are chosen. `GET /{id}/playlists` tells how
the playlists stand in that Jellyfin (as `GET /api/v1/plex/playlists` does for Plex, with `kind`), `POST
/{id}/playlists/sync` syncs them all now and looks for new ones there. nexmix gives itself one device id
per installation and server at Jellyfin (Jellyfin keeps one token per device id). Only the operator, only in a signed-in browser. A sign-in through plex.tv belongs to the
session that began it: for every other one, even of another operator, it does not exist (`pin_expired`); the same
holds for a Quick Connect of Jellyfin (`quick_expired`). No answer ever holds a token.

Music Assistant lives under `/api/v1/music-assistant`: `GET` the connected Music Assistant servers, each with its
`token` (`expires_at`, `days_left`, `warn` from 30 days before, `expired`), `providers` (how many are chosen),
`doubled` and `not_admin` (the token is no admin's any more: nothing is taken out or sorted there by place); `POST /connect` `{"url", "token"}` with a long-lived token made in Music Assistant (profile, tokens; it lasts
a year). The address is checked first (`/info`; `422 not_ma`, `ma_too_old` for a schema older than nexmix speaks,
`bad_url`, `address_refused`), then the token on a websocket of its own (`422 bad_token`; `422 ma_not_admin` for a token of a user who is no admin
there: nexmix must be able to cancel its tasks); `409 no_music_provider` when
it has none. The answer has the `server` and its music `providers` (`handle` of nexmix's own, `name`, `kind`, `chosen`);
a single provider is chosen at once. `GET /{id}`, `GET /{id}/providers` (with `doubled`: chosen providers that are a
Plex or Jellyfin server nexmix also knows directly, whose tracks would stand twice), `PUT /{id}/providers`
`{"handles"}` (only their tracks count; the catalogue is fetched anew), `GET /{id}/playlists` and `POST
/{id}/playlists/sync` (as for Jellyfin), `DELETE /{id}` lets go of it (`409 server_is_source` as above). Connecting the
same server again with a new token takes it up with its numbers. One websocket per Music Assistant carries every
question and its events.

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
  nightly sync (`catalog_full_at`), mail, backups, API keys); `GET /settings/readiness` ("Ready for the internet?",
  with a point `jellyfin` while one is connected: its token readable, its address inside the own network; a point
  `music_assistant` likewise, which warns from 30 days before its token runs out, with `days`),
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

## Every route

The whole list, by the first part of the path (the sections above say what each does and who may call it). A test
compares it with the routes of the server, both ways: a route that is not here, or one here the server does not have,
fails it.

- **about**: `GET /api/v1/about`, `GET /api/v1/about/updates`, `PUT /api/v1/about/updates`, `POST /api/v1/about/updates/check`
- **accounts**: `GET /api/v1/accounts`, `DELETE /api/v1/accounts/{account_id}`, `POST /api/v1/accounts/{account_id}/block`, `POST /api/v1/accounts/{account_id}/reset-link`, `PUT /api/v1/accounts/{account_id}/role`, `POST /api/v1/accounts/{account_id}/sign-out`, `POST /api/v1/accounts/{account_id}/totp/reset`, `POST /api/v1/accounts/{account_id}/unblock`
- **admin**: `GET /api/v1/admin/api-keys`, `POST /api/v1/admin/api-keys/{key_id}/block`
- **api-keys**: `GET /api/v1/api-keys`, `POST /api/v1/api-keys`, `DELETE /api/v1/api-keys/{key_id}`
- **auth**: `PUT /api/v1/auth/avatar`, `DELETE /api/v1/auth/avatar`, `POST /api/v1/auth/forgot`, `POST /api/v1/auth/login`, `POST /api/v1/auth/login/totp`, `POST /api/v1/auth/login/totp/cancel`, `POST /api/v1/auth/logout`, `POST /api/v1/auth/logout-all`, `GET /api/v1/auth/me`, `GET /api/v1/auth/methods`, `POST /api/v1/auth/passkey`, `POST /api/v1/auth/passkey/begin`, `GET /api/v1/auth/passkeys`, `POST /api/v1/auth/passkeys`, `POST /api/v1/auth/passkeys/begin`, `POST /api/v1/auth/passkeys/{uid}/remove`, `PUT /api/v1/auth/password`, `GET /api/v1/auth/sessions`, `DELETE /api/v1/auth/sessions/{uid}`, `POST /api/v1/auth/setup/codes`, `POST /api/v1/auth/setup/done`, `POST /api/v1/auth/totp/begin`, `POST /api/v1/auth/totp/confirm`, `POST /api/v1/auth/totp/disable`, `POST /api/v1/auth/totp/recovery`
- **avatars**: `GET /api/v1/avatars/{account_id}`
- **backups**: `GET /api/v1/backups`, `POST /api/v1/backups`, `POST /api/v1/backups/upload`, `DELETE /api/v1/backups/{name}`, `POST /api/v1/backups/{name}/check`, `POST /api/v1/backups/{name}/download`, `POST /api/v1/backups/{name}/restore`
- **catalog**: `GET /api/v1/catalog`, `GET /api/v1/catalog/albums/{album_id}`, `GET /api/v1/catalog/artists/{artist_id}`, `GET /api/v1/catalog/cover/{album_id}`, `GET /api/v1/catalog/facets`, `GET /api/v1/catalog/search`, `GET /api/v1/catalog/status`, `POST /api/v1/catalog/sync`
- **devices**: `GET /api/v1/devices`, `POST /api/v1/devices`, `DELETE /api/v1/devices/{uid}`
- **email**: `GET /api/v1/email/confirm/{token}`, `POST /api/v1/email/confirm/{token}`
- **guest**: `GET /api/v1/guest`, `GET /api/v1/guest/catalog`, `GET /api/v1/guest/catalog/search`, `GET /api/v1/guest/cover/{album_id}`, `GET /api/v1/guest/events`, `POST /api/v1/guest/items`, `POST /api/v1/guest/items/move`, `POST /api/v1/guest/items/remove`, `POST /api/v1/guest/items/restore`, `POST /api/v1/guest/leave`, `GET /api/v1/guest/suggestions`
- **health**: `GET /api/v1/health`
- **invite**: `GET /api/v1/invite/{token}`, `POST /api/v1/invite/{token}`
- **invites**: `GET /api/v1/invites`, `POST /api/v1/invites`, `DELETE /api/v1/invites/{invite_id}`
- **jellyfin**: `GET /api/v1/jellyfin`, `POST /api/v1/jellyfin/connect`, `POST /api/v1/jellyfin/quick`, `GET /api/v1/jellyfin/quick/{quick_id}`, `DELETE /api/v1/jellyfin/quick/{quick_id}`, `GET /api/v1/jellyfin/{server_id}`, `DELETE /api/v1/jellyfin/{server_id}`, `GET /api/v1/jellyfin/{server_id}/choices`, `PUT /api/v1/jellyfin/{server_id}/library`, `GET /api/v1/jellyfin/{server_id}/playlists`, `POST /api/v1/jellyfin/{server_id}/playlists/sync`, `PUT /api/v1/jellyfin/{server_id}/user`
- **join**: `GET /api/v1/join/{code}`, `POST /api/v1/join/{code}`, `GET /api/v1/join/{code}/cover/{album_id}`
- **locales**: `GET /api/v1/locales`, `GET /api/v1/locales/{code}`, `PUT /api/v1/locales/{code}`, `DELETE /api/v1/locales/{code}`
- **logs**: `GET /api/v1/logs`, `DELETE /api/v1/logs`, `GET /api/v1/logs/download`, `GET /api/v1/logs/level`, `PUT /api/v1/logs/level`
- **me**: `GET /api/v1/me`, `PUT /api/v1/me/email`, `DELETE /api/v1/me/email/pending`, `PUT /api/v1/me/language`, `PUT /api/v1/me/preferences`, `PUT /api/v1/me/profile`, `POST /api/v1/me/whats-new/seen`
- **music-assistant**: `GET /api/v1/music-assistant`, `POST /api/v1/music-assistant/connect`, `GET /api/v1/music-assistant/{server_id}`, `DELETE /api/v1/music-assistant/{server_id}`, `GET /api/v1/music-assistant/{server_id}/playlists`, `POST /api/v1/music-assistant/{server_id}/playlists/sync`, `GET /api/v1/music-assistant/{server_id}/providers`, `PUT /api/v1/music-assistant/{server_id}/providers`
- **oidc**: `DELETE /api/v1/oidc/admin/accounts/{account_id}/links/{provider_id}`, `GET /api/v1/oidc/admin/links`, `GET /api/v1/oidc/admin/providers`, `POST /api/v1/oidc/admin/providers`, `PUT /api/v1/oidc/admin/providers/order`, `PUT /api/v1/oidc/admin/providers/{provider_id}`, `DELETE /api/v1/oidc/admin/providers/{provider_id}`, `GET /api/v1/oidc/admin/providers/{provider_id}/impact`, `GET /api/v1/oidc/authentik/address`, `POST /api/v1/oidc/authentik/address`, `GET /api/v1/oidc/authentik/blueprint`, `POST /api/v1/oidc/authentik/setup`, `GET /api/v1/oidc/callback`, `GET /api/v1/oidc/me`, `POST /api/v1/oidc/me/address`, `GET /api/v1/oidc/providers`, `GET /api/v1/oidc/{slug}/callback`, `POST /api/v1/oidc/{slug}/link`, `DELETE /api/v1/oidc/{slug}/link`, `GET /api/v1/oidc/{slug}/start`
- **playlists**: `GET /api/v1/playlists`, `POST /api/v1/playlists`, `GET /api/v1/playlists/{playlist_id}`, `PUT /api/v1/playlists/{playlist_id}`, `DELETE /api/v1/playlists/{playlist_id}`, `DELETE /api/v1/playlists/{playlist_id}/concurrent`, `GET /api/v1/playlists/{playlist_id}/events`, `PUT /api/v1/playlists/{playlist_id}/hidden`, `DELETE /api/v1/playlists/{playlist_id}/hidden`, `GET /api/v1/playlists/{playlist_id}/invite`, `POST /api/v1/playlists/{playlist_id}/invite`, `PUT /api/v1/playlists/{playlist_id}/invite`, `DELETE /api/v1/playlists/{playlist_id}/invite`, `DELETE /api/v1/playlists/{playlist_id}/invite/guests/{guest_id}`, `POST /api/v1/playlists/{playlist_id}/items`, `POST /api/v1/playlists/{playlist_id}/items/move`, `POST /api/v1/playlists/{playlist_id}/items/remove`, `POST /api/v1/playlists/{playlist_id}/items/restore`, `GET /api/v1/playlists/{playlist_id}/mirrors`, `POST /api/v1/playlists/{playlist_id}/mirrors`, `POST /api/v1/playlists/{playlist_id}/mirrors/preview`, `DELETE /api/v1/playlists/{playlist_id}/mirrors/{server_id}`, `POST /api/v1/playlists/{playlist_id}/mirrors/{server_id}/emptied`, `PUT /api/v1/playlists/{playlist_id}/mirrors/{server_id}/entries/{entry_id}`, `DELETE /api/v1/playlists/{playlist_id}/mirrors/{server_id}/extras/{extra_id}`, `POST /api/v1/playlists/{playlist_id}/mirrors/{server_id}/extras/{extra_id}/take`, `POST /api/v1/playlists/{playlist_id}/mirrors/{server_id}/remake`, `DELETE /api/v1/playlists/{playlist_id}/missing`, `POST /api/v1/playlists/{playlist_id}/missing/remove`, `POST /api/v1/playlists/{playlist_id}/move`, `POST /api/v1/playlists/{playlist_id}/plex-order`, `DELETE /api/v1/playlists/{playlist_id}/plex-order`, `POST /api/v1/playlists/{playlist_id}/restore`, `GET /api/v1/playlists/{playlist_id}/shares`, `PUT /api/v1/playlists/{playlist_id}/shares/{account_id}`, `DELETE /api/v1/playlists/{playlist_id}/shares/{account_id}`, `POST /api/v1/playlists/{playlist_id}/sync`
- **plex**: `GET /api/v1/plex`, `DELETE /api/v1/plex`, `POST /api/v1/plex/connect`, `GET /api/v1/plex/libraries`, `PUT /api/v1/plex/library`, `POST /api/v1/plex/pin`, `GET /api/v1/plex/pin/{pin_id}`, `DELETE /api/v1/plex/pin/{pin_id}`, `GET /api/v1/plex/playlists`, `POST /api/v1/plex/playlists/sync`
- **preview**: `GET /api/v1/preview`, `GET /api/v1/preview/{track_id}`
- **push**: `GET /api/v1/push`, `POST /api/v1/push/devices`, `POST /api/v1/push/devices/lookup`, `PUT /api/v1/push/devices/{device_id}`, `DELETE /api/v1/push/devices/{device_id}`, `POST /api/v1/push/test`
- **reset**: `GET /api/v1/reset/{token}`, `POST /api/v1/reset/{token}`
- **servers**: `GET /api/v1/servers`, `DELETE /api/v1/servers/{server_id}`, `GET /api/v1/servers/{server_id}/transfer`, `POST /api/v1/servers/{server_id}/transfer`, `DELETE /api/v1/servers/{server_id}/transfer`, `GET /api/v1/servers/{server_id}/transfer/preview`, `POST /api/v1/servers/{server_id}/transfer/preview`
- **settings**: `GET /api/v1/settings`, `PUT /api/v1/settings`, `POST /api/v1/settings/mail-test`, `GET /api/v1/settings/push`, `PUT /api/v1/settings/push`, `POST /api/v1/settings/push/renew`, `GET /api/v1/settings/readiness`
- **setup**: `GET /api/v1/setup`, `POST /api/v1/setup`
- **storage**: `GET /api/v1/storage`
- **suggestions**: `GET /api/v1/suggestions`, `GET /api/v1/suggestions/hidden`, `POST /api/v1/suggestions/hidden`, `DELETE /api/v1/suggestions/hidden/{track_id}`, `GET /api/v1/suggestions/mood`, `GET /api/v1/suggestions/sonic`, `GET /api/v1/suggestions/together`
