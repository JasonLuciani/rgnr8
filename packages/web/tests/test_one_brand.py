"""One brand (issues 34 and 37).

The palette lived in three hand copies (Python, the TypeScript ledger-kernel,
the briefing email's CSS) that had drifted: brand.ts still set headings in a
serif and the email used retired greys. And brand sage, the accent, was used
as text on ivory at 3.9:1, under the 4.5:1 that body text needs.
"""

from __future__ import annotations

import re
from pathlib import Path

from rgnr8_forecast import brand

PACKAGES = Path(__file__).resolve().parents[2]


def _ts_palette() -> dict[str, str]:
    src = (PACKAGES / "ledger-kernel" / "src" / "brand.ts").read_text(encoding="utf-8")
    body = src[src.index("export const RG = {"):src.index("};", src.index("export const RG = {"))]
    return dict(re.findall(r'(\w+):\s*"(#[0-9A-Fa-f]{6})"', body))


def test_the_typescript_palette_is_the_python_palette() -> None:
    ts = _ts_palette()
    pairs = {"forest": brand.FOREST, "ivory": brand.IVORY, "sage": brand.SAGE, "ink2": brand.INK_2,
             "muted": brand.MUTED, "line": brand.LINE, "accentDeep": brand.ACCENT_DEEP,
             "positive": brand.POSITIVE, "watch": brand.WATCH, "risk": brand.RISK}
    for key, value in pairs.items():
        assert ts[key].upper() == value.upper(), (key, ts[key], value)


def test_no_copy_sets_type_in_a_serif_or_keeps_the_old_alias() -> None:
    for path in (PACKAGES / "ledger-kernel" / "src" / "brand.ts",
                 PACKAGES / "briefing" / "src" / "rgnr8_briefing" / "render.py",
                 PACKAGES / "reports" / "src" / "rgnr8_reports" / "render.py"):
        src = path.read_text(encoding="utf-8")
        assert "--rg-serif" not in src and "Georgia" not in src, path


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_every_text_colour_reads_on_both_grounds() -> None:
    for text in (brand.FOREST, brand.INK_2, brand.MUTED, brand.ACCENT_DEEP):
        for ground in (brand.IVORY, brand.PAPER):
            assert _contrast(text, ground) >= 4.5, (text, ground, round(_contrast(text, ground), 2))
    # ...which sage itself does not: it is for marks, rules and fills.
    assert _contrast(brand.SAGE, brand.IVORY) < 4.5


def test_no_screen_sets_text_in_sage() -> None:
    offenders = []
    for root in (PACKAGES / "web" / "src", PACKAGES / "ops" / "src"):
        for path in root.rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"(?<![-\w])color:var\(--rg-sage\)", line):
                    offenders.append(f"{path.name}:{n}")
    assert offenders == [], offenders
