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
(`secret.key`) together with the database: with both, the Plex token, the mail credentials and the client secrets of the
sign-in providers can be read. Keep backups as protected as the data folder. A device token never reaches the
operator's side, not even the operator's own.

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

## Plex

nexmix holds one Plex token. The usual way is the operator's sign-in at plex.tv, which hands it out; typing a server
address and a token by hand is the fallback for a server plex.tv does not list. Either way it is sealed with
the server's key the moment it is stored, sent to Plex in a header, never in an address, and never leaves the server: not
in an answer, not in the log, not in an error. The browser names tracks and albums by their number; the server builds
every request to Plex itself. Covers go through `photo/:/transcode` with fixed parameters only, for an album of the
catalogue; listening in goes through Plex's converter for a track of the catalogue, at most about 1 MB and 90 seconds,
one at a time per listener, 120 an hour, a few at once on the server. No address a browser sends is ever passed on.

The address of the Plex server is the one the operator chose; it is checked when it is set (no link-local or cloud
metadata address) and used as it is after that.

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

The server talks only to what is set up: the Plex server the operator chose, plex.tv while connecting, the mail server,
the sign-in providers, authentik during its one-step setup, the push services of the browsers, and api.github.com once a
day for the newest release (can be turned off). Nothing else, and never an address a request names.

Web Push targets are checked like every way out: resolved once and connected to exactly as checked, never link-local or
a cloud metadata address, no redirect followed, an answer read up to a few kilobytes, and one deadline for the whole
request, the answer read included.
