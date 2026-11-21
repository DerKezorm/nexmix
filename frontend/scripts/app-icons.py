"""The icons for the home screen, rendered once from the light version of the mark.

    python frontend/scripts/app-icons.py

Same shape as components/Logo.tsx and public/logo.svg; the source of every version is nexapps/icons-vergleich/neu.py.
Rendered with Playwright for Python (Chromium without a window).

The maskable icon is cropped by Android, so the mark sits smaller on a full white square. The Apple touch icon is full
bleed and without transparency: iOS rounds it itself. The badge of a notification is white on clear.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

PUBLIC = Path(__file__).resolve().parent.parent / "public"

VERLAUF = """<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" x1="10" y1="10" x2="54" y2="54">
  <stop offset="0" stop-color="#a78bfa"/><stop offset=".5" stop-color="#5b2ee6"/><stop offset="1" stop-color="#2e1680"/>
</linearGradient></defs>"""
ZEICHEN = """<path d="M13 18h27M13 27h21M13 36h14M13 45h10" stroke="url(#g)" stroke-width="4.2" stroke-linecap="round"/>
  <circle cx="43" cy="41" r="11" fill="#4a22c9" stroke="#fff" stroke-width="2"/>
  <path d="M40 35.8v10.4l8.6-5.2Z" fill="#fff" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/>"""


def abzeichen(s: int) -> str:
    """One colour, white on clear: Android tints everything that is not transparent. The play sign is cut out."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{s}" height="{s}"><defs><mask id="m">'
            '<rect width="64" height="64" fill="#fff"/><path d="M40 35.8v10.4l8.6-5.2Z" fill="#000" stroke="#000" '
            'stroke-width="1.4" stroke-linejoin="round"/></mask></defs><g fill="#fff" stroke="#fff" mask="url(#m)">'
            '<path d="M10 16h31M10 26h24M10 36h16M10 46h11" stroke-width="6" stroke-linecap="round" fill="none"/>'
            '<circle cx="44" cy="41" r="13" stroke="none"/></g></svg>')


def kachel(s: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{s}" height="{s}">{VERLAUF}'
            '<rect x="2" y="2" width="60" height="60" rx="16" fill="#fff"/>'
            '<rect x="2.75" y="2.75" width="58.5" height="58.5" rx="15.3" fill="none" stroke="#5b2ee6" stroke-opacity=".35" '
            f'stroke-width="1.5"/>{ZEICHEN}</svg>')


def voll(s: int, m: float) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{s}" height="{s}">{VERLAUF}'
            f'<rect width="64" height="64" fill="#fff"/><g transform="translate(32 32) scale({m}) translate(-32 -33)">{ZEICHEN}</g></svg>')


def randlos(s: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="2 2 60 60" width="{s}" height="{s}">{VERLAUF}'
            f'<rect x="2" y="2" width="60" height="60" fill="#fff"/>{ZEICHEN}</svg>')


BILDER = [
    ("icon-192.png", kachel(192), 192, False),
    ("icon-512.png", kachel(512), 512, False),
    ("icon-maskable-512.png", voll(512, 0.82), 512, True),
    ("apple-touch-icon.png", randlos(180), 180, True),
    ("badge-96.png", abzeichen(96), 96, False),
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, svg, size, opaque in BILDER:
            page = browser.new_page(viewport={"width": size, "height": size})
            page.set_content(f'<body style="margin:0;background:transparent">{svg}</body>')
            bild = Image.open(io.BytesIO(page.locator("svg").screenshot(omit_background=True)))
            page.close()
            assert bild.size == (size, size), (name, bild.size)
            (bild.convert("RGB") if opaque else bild.convert("RGBA")).save(PUBLIC / name, optimize=True)
            print("written:", PUBLIC / name)
        browser.close()


if __name__ == "__main__":
    main()
