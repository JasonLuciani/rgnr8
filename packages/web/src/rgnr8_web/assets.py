"""Static files this app serves from its own origin — today, the typeface.

RGNR8 ships **no external assets**, and a test asserts that no `http(s)` URL
appears in any rendered page. That rules out a font CDN, so a self-hosted
typeface needs a static route, and a static route is the classic place to leak
a filesystem.

This one cannot leak anything. The served set is a dict built **at import time**
from a fixed tuple of filenames: there is no path joining at request time, no
`..` to normalise, no symlink to follow, and no way to name a file that isn't in
the tuple. An unknown key is simply absent from the dict.

Each URL carries a content hash, so the response is immutable-cacheable for a
year and a changed font necessarily ships under a changed URL — no stale-cache
window, no cache-busting query string to forget.

Why a webfont at all: the product mixed `system-ui` with Georgia for headings,
which meant it rendered as a different product on macOS, Windows and Android,
and read as a document rather than an instrument. One typeface fixes that. Inter
is the choice because this is a financial tool — the screens are dense with
figures, and Inter's tabular numerals, tall x-height and disambiguated `1/l/I`
and `0/O` are what make a column of money legible at 13px. It is SIL OFL, and
`assets/Inter-OFL.txt` ships alongside it as that licence requires.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "assets"

# The ONLY files this module will ever serve. Adding one is a code change and a
# code review; it is deliberately not driven by anything at request time.
_FILES: tuple[tuple[str, str, str], ...] = (
    # (stem, extension, content type)
    ("inter-latin", "woff2", "font/woff2"),
    ("inter-latin-ext", "woff2", "font/woff2"),
)

# A year. Safe only because the URL is content-addressed: different bytes can
# never appear at this URL, so nothing downstream can serve a stale font.
_MAX_AGE = 31_536_000


@dataclass(frozen=True, slots=True)
class Asset:
    name: str          # the URL segment, e.g. "inter-latin.3100e775e861.woff2"
    body: bytes
    content_type: str


def _load() -> dict[str, Asset]:
    out: dict[str, Asset] = {}
    for stem, ext, ctype in _FILES:
        raw = (_DIR / f"{stem}.{ext}").read_bytes()
        name = f"{stem}.{hashlib.sha256(raw).hexdigest()[:12]}.{ext}"
        out[name] = Asset(name=name, body=raw, content_type=ctype)
    return out


ASSETS: dict[str, Asset] = _load()

#: Where `_asset_response` is mounted. Kept here so the CSS and the route can
#: never disagree about it.
ASSET_PREFIX = "/assets/"

CACHE_HEADER = f"public, max-age={_MAX_AGE}, immutable"


def asset_url(stem: str) -> str:
    """The content-addressed URL for a loaded asset stem."""
    for name in ASSETS:
        if name.startswith(f"{stem}."):
            return f"{ASSET_PREFIX}{name}"
    raise KeyError(stem)


def font_face_css() -> str:
    """`@font-face` for the self-hosted typeface, with the hashed URLs.

    Two subsets rather than one file: `latin` alone is ~48KB and covers every
    screen in the product, while `latin-ext` (~85KB) is fetched **only** if a
    page actually renders a character in its range — an accented vendor name, a
    currency symbol outside Latin-1. Browsers honour `unicode-range` before
    downloading, so the common case pays for neither.

    `font-display:swap` is deliberate: text must be readable during the fetch.
    A financial screen that renders blank for 3s is worse than one that reflows.
    """
    latin = asset_url("inter-latin")
    latin_ext = asset_url("inter-latin-ext")
    return (
        # One variable file spans 400-800, so every weight the UI uses comes
        # from a single download rather than four static faces.
        "@font-face{font-family:'Inter';font-style:normal;font-weight:400 800;"
        f"font-display:swap;src:url({latin_ext}) format('woff2');"
        "unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,"
        "U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,"
        "U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}"
        "@font-face{font-family:'Inter';font-style:normal;font-weight:400 800;"
        f"font-display:swap;src:url({latin}) format('woff2');"
        "unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,"
        "U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,"
        "U+2193,U+2212,U+2215,U+FEFF,U+FFFD}"
    )
