"""RGNR8 brand system — palette lock + cross-language parity with brand.ts."""

from pathlib import Path

from rgnr8_forecast import brand


def test_palette_is_the_rgnr8_guide() -> None:
    assert brand.FOREST == "#0E241E"
    assert brand.IVORY == "#F2EFE6"
    assert brand.SAGE == "#5E7F63"
    assert brand.INK == "#0E241E"
    assert brand.ACCENT == "#5E7F63"
    assert brand.POSITIVE == "#3E7C5A"
    assert brand.WATCH == "#B7791F"
    assert brand.RISK == "#B4443C"


def test_status_color_maps_levels() -> None:
    assert brand.status_color("STABLE") == brand.POSITIVE
    assert brand.status_color("AT RISK") == brand.RISK
    assert brand.status_color("watch") == brand.WATCH


def test_brand_bar_renders_wordmark_and_context() -> None:
    bar = brand.brand_bar("Cash outlook")
    assert 'class="rg-wordmark"' in bar
    assert 'RGNR<span class="rg-8">8</span>' in bar
    assert "Cash outlook" in bar
    assert "&lt;x&gt;" in brand.brand_bar("<x>")  # escapes context


def test_theme_css_is_self_contained() -> None:
    assert "http://" not in brand.THEME_CSS
    assert "https://" not in brand.THEME_CSS
    assert "--rg-sage:#5E7F63" in brand.THEME_CSS


def test_infinity_mark_renders_inline_svg() -> None:
    m = brand.mark_svg(22)
    assert m.startswith("<svg") and "</svg>" in m
    assert brand.SAGE in m
    assert "http" not in m  # self-contained


def test_parity_with_typescript_brand_tokens() -> None:
    """The TS mirror (`@rgnr8/ledger-kernel` brand.ts) must carry identical hexes."""
    ts = Path(__file__).resolve().parents[2] / "ledger-kernel" / "src" / "brand.ts"
    if not ts.exists():  # not laid out side-by-side (e.g. packaged) — skip
        return
    text = ts.read_text()
    for hexcode in (brand.FOREST, brand.IVORY, brand.SAGE, brand.POSITIVE, brand.WATCH,
                    brand.RISK, brand.INK_2, brand.MUTED, brand.LINE, brand.ACCENT_DEEP):
        assert hexcode in text, f"{hexcode} missing from brand.ts — palette drift"
