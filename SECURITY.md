# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub instead:
**Security, Report a vulnerability** on this repository (private vulnerability reporting). Say what you found, how to
reproduce it, and which version you ran.

You get an answer within a week. A fix goes out as a new release; the release notes name the problem once the fix is
available, and you are thanked there unless you would rather not be.

## Supported versions

Only the newest release gets security fixes. Updating is `docker compose pull && docker compose up -d`; see
[Updating](README.md#updating).

## Reaching nexmix from outside

Some run nexmix on the internet, so it is built for that: put it behind a reverse proxy with https, set
`NEXMIX_PUBLIC_URL` and `NEXMIX_TRUSTED_PROXIES`, and look at "Ready for the internet?" in Settings, Sign-in, which checks
https, the second factor, the protection against guessing, the server secrets, the cookies, the proxy and the backups.
A second factor is not required from the start, so the first account gets in at once; before nexmix is reachable from
the internet, set up your own and turn on "Require a second factor" in Settings, Sign-in.
Set the server up first, from your own network, with the setup code from the log.

## What the operator can reach

The operator manages accounts, the settings and the backups. A backup holds the server's secret
(`secret.key`) together with the database: with both, every token of a media server (Plex, Jellyfin, Music Assistant,
among them keys with the rights of an administrator there, see [Media servers](#media-servers)), the mail credentials
and the client secrets of the sign-in providers can be read. Keep backups as protected as the data folder, and as the
media servers themselves. A device token never reaches the operator's side, not even the operator's own.

## Who sees what

Every route checks the right of the one who asks, on the server, at every request; the tests walk the list of routes for
every kind of caller (nobody, a guest, a member, the operator, a device token, an API key), so a route added later is
covered too.

- **A member** sees the own playlists and the ones shared with them, to look at or to change; a playlist they may not
  see answers `404`, never "forbidden", so its existence does not leak. Deleting, bringing back and sharing are the
  owner's and the operator's. The operator sees all playlists.
- **A guest** holds a cookie for one invitation and reaches that one playlist, the search, suggestions, covers and, where
  the invitation allows it, listening in: no other route, whatever it sends. The cookie is `HttpOnly`, `SameSite=Lax`,
  only for `/api/v1`, and worth nothing as soon as the invitation ends, the guest is taken out, or the person who invited
  may no longer change the playlist. The code of an invitation has 12 characters (31^12, about 8·10^17), is kept as an
  HMAC with the server's key, and is masked in the log. Wrong codes are braked per sender (behind a trusted proxy per
  real sender); a wrong code never holds up a right one, so a sender who guesses cannot lock the guests of a party out.
  Whoever has the code can: joins are braked per invitation (300 in ten minutes, at most 300 guests at once), and from
  one sender only 60 in ten minutes, so one browser cannot fill the door, but many addresses with the code can. Then
  the host ends the invitation and makes a new one: the old code is dead at once, and guests who should stay get the
  new link.
- **An API key** only reads, as its account; it never changes anything and never reaches an invitation.
- **A device token** acts as its account, but never on the operator's side.

## Media servers

nexmix holds a token for every media server it is connected to, and what each one may do there is more than nexmix
needs to read:

- **Plex**: the token of the Plex account, from the operator's sign-in at plex.tv (typing a server address and a token
  by hand is the fallback for a server plex.tv does not list). It reaches whatever that account reaches.
- **Jellyfin**: the token of a Jellyfin user from Quick Connect, or an **API key**. An API key of Jellyfin has the full
  rights of an administrator there; Quick Connect with a user of its own who is no administrator is the narrower way and
  does everything nexmix needs.
- **Music Assistant**: a long-lived token whose user has the **admin role**. Music Assistant has no role in between,
  and nexmix needs this one to cancel a change of its own that it gave up waiting for; without it, taking entries out by
  their place could later hit the wrong track. A token of any other user is refused when connecting. Such a token
  reaches all of Music Assistant: speakers, settings, providers.

This is a choice made on purpose: whoever takes over nexmix, or a backup with its `secret.key`, holds these keys too.
Every token is sealed with the server's key the moment it is stored, sent in a header or inside the websocket, never in
an address, and never leaves the server: not in an answer, not in the log, not in an event, not in an error. The browser
names tracks, albums and playlists by nexmix's numbers; the server builds every request to a media server itself.
Covers go through each server's scaler with fixed sizes only, for an album of the catalogue (Plex: `photo/:/transcode`
with fixed parameters; Jellyfin: an id of 32 hexadecimal characters; Music Assistant: `/imageproxy` with an id of 64);
listening in goes through the server's converter for a track of the catalogue, at most about 1 MB and 90 seconds, one
at a time per listener, 120 an hour, a few at once on the server. Jellyfin and Music Assistant serve sound and pictures
without signing in: nexmix fetches them itself and never hands an id, an address or a token of a preview to a browser
or a guest. No address a browser sends is ever passed on.

What a media server answers is not trusted either: every answer is read up to its limit and no further (a list 64 MB,
a cover 2 MB, the info of Music Assistant 64 KB, a message on a websocket 32 MB for Music Assistant and 4 MB for
Jellyfin, an answer of Music Assistant in several parts 32 MB in all, a line of Plex's event stream 1 MB), names and
texts are cut, and a server that says "read everything again" over and over asks for a full run of the catalogue at
most every five minutes.

The address of each server is the one the operator chose. Its name is checked when it is set and **again at every
request and every websocket**: it is looked up, every address it gives is checked (never link-local, never a cloud
metadata address), and the connection goes to exactly those addresses. A name that later points somewhere refused is
not reached. Addresses inside your own network are allowed on purpose: that is where media servers live.

## Limits

Requests have a size limit (`413 too_large`), lists and searches a largest page, uploads (pictures, backups, languages)
their own limits, read while the bytes come and never taken from what a file says about itself. Every request the
server makes has a deadline. The brakes against guessing, listening in and guests keep their counts in memory; a
restart forgets them, which is fine for a brake.

## The door before the sign-in

A request that cannot be anybody's is answered before the framework or the database is asked: no credential at all (no
session cookie, no `Authorization: Bearer`) on a route that wants a sign-in is a 401 at once. A sender whose wrong guesses
keep coming (thirty a minute) is turned away for every credential the database does not know, and **never for one it
does**: a session, a device token or an API key that exists goes through, after a restart and behind an address that a
whole household shares. A device token or API key is looked up before the brake against guessing is asked, and the same
wrong one again counts once, so a revoked token that an app keeps sending shuts nobody out. Guessing a new one every time
still meets the brake. A lock after wrong passwords is for the password: it does not cut off the tokens and keys of the
account.

## Backups, and where the data lies

**A backup holds the database, the languages the operator added and `secret.key`, not the music.** It is made with SQLite's backup API (a file copy of a
database in WAL mode would miss what is not yet in the main file). Downloading, deleting, uploading and restoring one
ask for the operator's password again, from a browser only: a stolen session or a device token can neither carry a
backup away nor lay one out. An archive that is uploaded or restored is read with its limits: it may hold nothing but
its parts (a path, a link or another name makes it unusable); the manifest is read up to 64 KB and no further; the
bytes are counted while they are unpacked and never taken from what the archive says about itself; the database must
be the one the manifest names by size and sha256 (a flipped byte is "damaged", not an error).

**How the trial run opens the database of an archive.** It never opens it in place and never with the server's own
connection. The database is unpacked into a hidden scratch file in the backups folder (`.check-<process>-<time>.db`,
removed when the run ends, also after an error) and looked at in this order. (1) `PRAGMA integrity_check` and
`user_version` through a plain `sqlite3` connection; no extension can be loaded and nothing is attached. (2) A read only
connection (`mode=ro`) lists the triggers, views and virtual tables in `sqlite_master`; any that nexmix does not make
itself ends the run, **before anything is written**, because a trigger would fire at every write of the app after a
restore, a view may name anything and a virtual table has a module of SQLite behind it. Plain tables of data nexmix
does not know are left alone: nothing reads them. (3) Only then is the scratch copy written to, as the start does it:
the steps that bring it to today's schema, the end of the stored sign-ins, and a look (read only again) that every table
and column nexmix needs is there. Any error in any of it, or a database without tables, makes the archive "not
usable", logs the kind of the error (never its text, which could quote the database) and changes nothing. The scratch
copy is the only file that is changed. Because the real start does the same steps, an archive that passes cannot stop
the next start.

**A restore that fails.** The swap at the start is made on temporary files beside the live ones and ends in `os.replace`;
the live database and `secret.key` are not touched before that. If anything fails, the staged files move to
`backups/restore-failed/` (with a note of when and what kind of error), the log and the operators (push, and a mail with
a mail server) say so, and the server starts with the database it had. The way back is also kept: a restore makes a copy
of the state before it first.

**What a restore brings back.** It ends every session, but a backup does not know what was ended after it was made:
devices and API keys that were revoked, blocked or removed since are valid again after it. The trial run counts them
from the live database (`revived_devices`, `revived_keys`) and says so in the dialog; after a restore, check the lists
of devices and keys and withdraw what should stay gone. A password changed since is the old one again, too.

**SQLite on a network share.** NFS and SMB do not keep SQLite's promises, and a database there can lose data without a
word. nexmix looks where the data folder lies (the mount table on Linux, the drive type on Windows), says so on the
storage card and in the log at the start, and does not stop: the decision stays with the operator.

## Addresses the server reaches out to

The server talks only to what is set up: the media servers the operator chose (Plex; Jellyfin over HTTP and its
websocket `/socket`; Music Assistant at `/info`, its websocket `/ws`, `/imageproxy` and `/preview`), plex.tv while
connecting, the mail server, the sign-in providers, authentik during its one-step setup, the push services of the
browsers, and api.github.com once a day for the newest release (can be turned off). Nothing else, and never an address
a request names. The name of a media server is looked up and checked at every connection, as above.

Web Push targets are checked like every way out: resolved once and connected to exactly as checked, never link-local or
a cloud metadata address, no redirect followed, an answer read up to a few kilobytes, and one deadline for the whole
request, the answer read included.
