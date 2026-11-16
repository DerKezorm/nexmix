# Third-party licences

nexmix itself is licensed under the **GNU Affero General Public License v3.0** (see [LICENSE](LICENSE)). This file
lists what it ships or depends on.

## Bundled with the app

These files travel inside the container image, so their notices travel with them.

| What | Copyright | Licence | Notice in the app |
|---|---|---|---|
| Fonts Inter and Space Grotesk (via Fontsource) | The Inter and Space Grotesk Project Authors | OFL-1.1 | `/licenses/fonts.txt` |
| Lucide icons (partly from Feather) | Lucide Contributors; Cole Bemis | ISC, MIT | `/licenses/lucide.txt` |

nexmix loads no font, icon or script from another host.

## Backend

What `backend/requirements.txt` names, and what those packages bring along (checked against the installed set, which is
what the container image installs; on Linux `uvicorn[standard]` adds uvloop). websockets is named directly since 0.2:
nexmix speaks to Jellyfin and Music Assistant over their websockets.

| Package | Licence |
|---|---|
| FastAPI, Starlette (the web framework), SQLAlchemy, pydantic, pydantic-core, pydantic-settings, annotated-types, annotated-doc, typing-inspection | MIT, Starlette BSD-3-Clause |
| anyio, h11, httptools, watchfiles, PyYAML, PyJWT, http-ece | MIT |
| argon2-cffi, argon2-cffi-bindings, cffi | MIT (cffi: MIT-0 for its own part) |
| uvicorn, httpx, httpcore, click, idna, segno, python-dotenv, websockets, pycparser | BSD-3-Clause |
| uvloop (Linux only) | MIT **or** Apache-2.0 |
| Pillow (13) | MIT-CMU |
| cryptography | Apache-2.0 **or** BSD-3-Clause |
| tzdata (the IANA time zone database for Python) | Apache-2.0; the data itself is in the public domain |
| typing-extensions | PSF-2.0 |
| SQLite (the library inside Python) | Public domain |
| certifi (the CA bundle) | MPL-2.0; the file is unchanged and its source is public |
| pillow-heif | BSD-3-Clause for its own code; the binary wheels are **GPLv2** as a whole, see below |
| webauthn (py_webauthn, passkeys) | BSD-3-Clause |
| cbor2 | MIT |
| pyOpenSSL | Apache-2.0 |
| pyasn1, pyasn1-modules | BSD-2-Clause, BSD-3-Clause |
| packaging | Apache-2.0 or BSD-2-Clause |

### HEIC photos: pillow-heif and the libraries it brings

To read iPhone photos (HEIC) as a profile picture, nexmix uses pillow-heif. Its binary wheels, and so the container
image, contain:

| Library | Licence |
|---|---|
| libheif | LGPL-3.0 |
| libde265 | LGPL-3.0 |
| x265 | GPL-2.0-or-later |

The wheel ships these notices in `pillow_heif-*.dist-info/licenses/`. GPL-2.0-or-later and LGPL-3.0 may be combined
with an AGPL-3.0 work, and the source of every part is public.

### Pictures: Pillow and the libraries it brings

To read profile pictures (JPEG, PNG, GIF, BMP, and the WebP and AVIF that Android phones, newer cameras and browsers
save), nexmix uses Pillow 13, whose binary wheels carry the decoders. Their notices are in
`pillow-*.dist-info/licenses/LICENSE` of the wheel:

| Library | Licence |
|---|---|
| libwebp | BSD-3-Clause |
| libavif (with the AV1 decoder dav1d and, in the wheels that carry it, aom) | BSD-2-Clause |
| libjpeg-turbo | IJG, BSD-3-Clause, zlib |
| libpng | libpng-2.0 |
| zlib-ng | zlib |
| libtiff | libtiff |
| openjpeg | BSD-2-Clause |
| lcms2, harfbuzz, brotli | MIT |
| freetype | FreeType License (FTL) **or** GPL-2.0-or-later |
| xz | 0BSD (liblzma) |

## Frontend

| Package | Licence |
|---|---|
| React, React DOM, React Router, i18next, react-i18next, scheduler, cookie, set-cookie-parser, html-parse-stringify, void-elements, use-sync-external-store, @babel/runtime | MIT |
| lucide-react | ISC |
| Fontsource packages | MIT (the fonts themselves OFL-1.1, see above) |

Build and test tools (Vite, TypeScript, Tailwind CSS, Vitest, ruff, pytest) are not part of the image. Their code is not
in the finished interface either, except what Vite and Tailwind CSS (both MIT) compile into the files.

## The base image

The container starts from `python:3.13-slim` (Debian GNU/Linux; the Python Software Foundation licence for Python, and the
licences of the Debian packages, listed in `/usr/share/doc/` of the image). On top of it: `curl` (the curl licence, an MIT
style licence; for the health check) and `gosu` (Apache-2.0; drops the rights at the start). Nothing is downloaded when
the container runs.

## Compatibility

All of the above may be combined into an AGPL-3.0 work. The obligation runs one way: nexmix as a whole is
AGPL-3.0, and anyone who runs a modified version as a network service must offer its source.
