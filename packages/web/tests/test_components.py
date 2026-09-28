"""The component vocabulary, and the rule that keeps it the only vocabulary.

The most valuable test here is `test_every_class_the_renderers_use_is_defined`.
It exists because `.grid`, `.grid2`, `.grid3` and `.grid4` were named by roughly
eighty call sites and defined by nothing the app served: every "grid" form in
the product had silently been rendering as a plain vertical stack, and the
fifty-one `grid-column:1/-1` spans on their submit buttons were inert. Six
hundred and seventy passing tests did not notice, because every one of them
asserted on markup and none of them asked whether the markup meant anything.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rgnr8_forecast.brand import RG_BASE_CSS
from rgnr8_web.components import (
    Action,
    TimelineEntry,
    alloc_editor,
    empty_row,
    empty_state,
    form_section,
    metric,
    page_header,
    queue,
    status_chip,
    timeline,
)
from rgnr8_web.shell import _SHELL_CSS

SRC = Path(__file__).resolve().parents[1] / "src" / "rgnr8_web"


# --- the rule ---------------------------------------------------------------


def _defined_classes(css: str) -> set[str]:
    """Every class name the stylesheet defines a rule for."""
    return set(re.findall(r"\.([A-Za-z][\w-]*)", css))


def _used_classes(text: str) -> set[str]:
    """Every literal class name a renderer writes into its markup.

    Only literals: a name built from an f-string is invisible here, which is a
    limit worth having rather than a false sense of coverage.
    """
    out: set[str] = set()
    for attr in re.findall(r'class="([^"{}]*)"', text):
        out.update(t for t in attr.split() if t)
    return out


# Classes that legitimately have no rule of their own: structural hooks reached
# through a descendant selector (`.tile .k`, `.queue .ask`) rather than named
# directly. Anything else in this set would be a class that renders as nothing.
_EXEMPT = {
    "k", "v", "t", "s", "fill", "track", "what", "how", "ask", "acts", "when",
    "who", "line", "note", "why", "h", "m", "row-item", "rg-8",
}


def test_every_class_the_renderers_use_is_defined() -> None:
    # A module may carry its own <style> (the financial package and the legal
    # pages are standalone documents), so a class counts as defined if the
    # shared stylesheet OR that module's own defines it.
    shared = _defined_classes(_SHELL_CSS)
    missing: dict[str, set[str]] = {}
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        own = _defined_classes(text) if "<style>" in text else set()
        gap = _used_classes(text) - shared - own - _EXEMPT
        if gap:
            missing[path.name] = gap
    assert not missing, (
        "these classes are written into markup but no rule defines them, so "
        "they render as nothing — which is exactly how every `.grid` form in "
        f"the app came to be a plain stack: {missing}"
    )


def test_the_field_grids_are_actually_grids() -> None:
    # The specific regression. `.grid` has to *be* a grid in the stylesheet the
    # app serves, not just a word in a class attribute.
    for name in ("grid", "grid2", "grid3", "grid4"):
        rule = re.search(rf"\.{name}\{{([^}}]*)\}}", RG_BASE_CSS)
        assert rule, f".{name} is used by the renderers and must be defined"
        assert "display:grid" in rule.group(1), f".{name} must be display:grid"
    assert re.search(r"\.wide\{[^}]*grid-column:1/-1", RG_BASE_CSS), (
        "`.wide` is what fifty-one submit buttons use to span their form"
    )


def test_the_grids_collapse_on_a_phone() -> None:
    # An owner approving a bill on a phone must not scroll sideways.
    narrow = RG_BASE_CSS.split("@media(max-width:560px)", 1)
    assert len(narrow) == 2, "the field grids need a phone breakpoint"
    assert "grid-template-columns:minmax(0,1fr)" in narrow[1][:200]


def test_buttons_do_not_underline_themselves() -> None:
    # `.btn` is worn by <a> as often as by <button>. This is the same defect
    # that rendered the wordmark underlined in #0000EE on the forest bar.
    rule = re.search(r"\.btn,button\{([^}]*)\}", RG_BASE_CSS)
    assert rule and "text-decoration:none" in rule.group(1)


def test_a_plain_link_is_not_browser_blue() -> None:
    # Also found by looking at a rendered page: nothing styled a bare <a>, so
    # "View 2026-08 package" was #0000EE on ivory, and purple once visited.
    assert re.search(r"(^|\n)a\{color:var\(--rg-accent-deep\)\}", RG_BASE_CSS)
    assert re.search(r"a:visited\{color:var\(--rg-accent-deep\)\}", RG_BASE_CSS)


def test_the_empty_row_is_defined_once() -> None:
    # Sixteen renderers hand-wrote `padding:24px`, the shell said 32px and the
    # operator console said 26px. One rule now.
    assert re.search(r"\.empty\{[^}]*text-align:center", RG_BASE_CSS)
    assert "text-align:center;padding:24px" not in _SHELL_CSS


# --- the components ---------------------------------------------------------


def test_page_header_escapes_and_places_everything() -> None:
    html = page_header(
        "Cash <script>", sub="Acme & Co", eyebrow="Books",
        actions=(Action("Export", href="/x?a=1&b=2"),),
    )
    assert "<script>" not in html
    assert "Cash &lt;script&gt;" in html
    assert "Acme &amp; Co" in html
    assert 'href="/x?a=1&amp;b=2"' in html
    assert 'class="page-head"' in html and 'class="acts"' in html


def test_metric_hero_is_opt_in_and_tone_colours_only_the_figure() -> None:
    plain = metric("Runway", "14 weeks")
    assert 'class="metric"' in plain and "hero" not in plain
    hero = metric("Cash today", "$128,400.00", hero=True, tone="risk")
    assert 'class="metric hero"' in hero
    assert 'class="v risk"' in hero
    # The label must not inherit the tone — a red "CASH TODAY" shouts at an
    # owner about a figure that is merely low.
    assert 'class="k"' in hero and 'class="k risk"' not in hero


@pytest.mark.parametrize(
    ("status", "tone", "glyph"),
    [("STABLE", "pos", "✓"), ("WATCH", "watch", "◆"),
     ("AT_RISK", "risk", "▲"), ("at risk", "risk", "▲")],
)
def test_status_chip_never_rides_on_colour_alone(status: str, tone: str, glyph: str) -> None:
    html = status_chip(status)
    assert f"chip-status {tone}" in html
    assert glyph in html, "a chip must carry a glyph, not just a colour"
    assert 'aria-label="Status: ' in html


def test_an_unknown_status_is_neutral_rather_than_reassuring() -> None:
    # Inventing a colour for a state nobody defined is how a screen ends up
    # claiming everything is fine.
    html = status_chip("HALF_CLOSED")
    assert "chip-status neutral" in html
    assert "pos" not in html


def test_empty_row_spans_the_table_and_says_what_to_do() -> None:
    html = empty_row(7, "No invoices yet", how="Create one from a job.")
    assert 'colspan="7"' in html and 'class="empty"' in html
    assert "No invoices yet" in html and "Create one from a job." in html


def test_empty_state_escapes_its_copy() -> None:
    assert "<b>" not in empty_state("No <b>bills</b>")


def test_the_empty_states_call_to_action_is_a_button_not_link_coloured_text() -> None:
    # Found by rendering it and looking: the actions used to inherit the empty
    # state's link colour, painting sage text onto a sage button — an invisible
    # label on the one control the screen exists to offer.
    html = empty_state("Nothing to reconcile", actions=(Action("Connect a bank", href="#", kind="sage"),))
    assert 'class="acts"' in html and 'class="how"' not in html.split("acts")[0][-40:]
    assert re.search(r"\.empty \.how a:not\(\.btn\)", RG_BASE_CSS), (
        "the link colour must not reach a button inside an empty state"
    )


def test_a_button_with_no_class_is_still_a_button() -> None:
    # Fifty-one submit controls ship with no class and rendered as the OS grey
    # default next to RGNR8's own buttons on the same form.
    rule = re.search(r"\.btn,button\{([^}]*)\}", RG_BASE_CSS)
    assert rule, "the bare element needs the button look, not just the class"
    assert "background:var(--rg-forest)" in rule.group(1)


def test_queue_returns_nothing_for_nothing() -> None:
    # So a caller can fall back to an empty state without first counting.
    assert queue([]) == ""
    html = queue([("Approve $4,200 to Ace Supply", "Due Friday",
                   (Action("Approve", on_click="approve('b1')"),))])
    assert 'class="queue"' in html and "Approve $4,200 to Ace Supply" in html


def test_form_section_uses_a_real_grid_and_rejects_a_made_up_one() -> None:
    assert 'class="grid3"' in form_section("Pay", "<label>x</label>", cols="grid3")
    assert 'class="grid"' in form_section("Pay", "<label>x</label>", cols="grid9")


def test_alloc_editor_flags_a_split_that_does_not_reconcile() -> None:
    ok = alloc_editor([("Job A", "<input>", 60.0), ("Job B", "<input>", 40.0)])
    assert 'class="alloc-total"' in ok and "over" not in ok
    bad = alloc_editor([("Job A", "<input>", 60.0), ("Job B", "<input>", 25.0)])
    assert 'class="alloc-total over"' in bad
    assert "of 100%" in bad, "an owner has to see what it should have summed to"


def test_alloc_bars_cannot_escape_their_track() -> None:
    html = alloc_editor([("Job A", "<input>", 320.0), ("Job B", "<input>", -50.0)])
    assert "width:100%" in html and "width:0%" in html
    assert "width:320" not in html and "-50" not in html.split("alloc-total")[0]


def test_timeline_preserves_the_order_it_was_given() -> None:
    # An audit trail is evidence. A renderer that reorders evidence is a
    # renderer that can hide something.
    entries = [
        TimelineEntry("2026-03-02", "Published February package", "ana@acme.com"),
        TimelineEntry("2026-01-09", "Locked January", "jo@acme.com"),
        TimelineEntry("2026-02-14", "Reopened January", "jo@acme.com"),
    ]
    html = timeline(entries)
    order = [m for m in re.findall(r"2026-\d\d-\d\d", html)]
    assert order == ["2026-03-02", "2026-01-09", "2026-02-14"]
    assert timeline([]) == ""


def test_action_renders_a_link_or_a_button_but_escapes_either() -> None:
    link = Action("Go", href='/t/a"onmouseover=x').html()
    assert '"onmouseover' not in link.replace("&quot;", "")
    btn = Action("Run", on_click="doThing('a')").html()
    assert "<button" in btn and "doThing(&#x27;a&#x27;)" in btn


# --- the ratchet ------------------------------------------------------------

# Inline styles across every package, at the moment this vocabulary landed.
# It was 471 before; naming the components that already existed in spirit —
# `.grid`, `.wide`, `.empty`, `.small`, `.btn-link`, `.btn.danger` — took out a
# hundred and twenty-nine of them. This is a ceiling, not a target: lower it
# whenever you remove more, and never raise it. A new inline style means a
# component is missing. Name it in RG_BASE_CSS instead.
_INLINE_STYLE_CEILING = 342


def test_inline_styles_only_ever_go_down() -> None:
    root = SRC.parents[2]  # packages/
    total = 0
    for path in sorted(root.glob("*/src/**/*.py")):
        total += len(re.findall(r'style="', path.read_text(encoding="utf-8")))
    assert total <= _INLINE_STYLE_CEILING, (
        f"{total} inline styles, up from {_INLINE_STYLE_CEILING}. Every one of "
        "these is a style that cannot be changed in one place — which is how "
        "the same table ended up padded two different ways. Name the component "
        "in RG_BASE_CSS instead."
    )
    # Keep the ceiling honest: if it drifts far above reality, it stops being a
    # ratchet and becomes a rubber stamp.
    assert total >= _INLINE_STYLE_CEILING - 40, (
        f"only {total} inline styles left — lower _INLINE_STYLE_CEILING to "
        f"{total} so the ratchet keeps biting."
    )
