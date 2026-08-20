"""Number-provenance labels: the ladder, badges, legend, and their use on the
books statements screen."""

from rgnr8_web.books_screens import render_books_statements
from rgnr8_web.provenance_labels import (
    Provenance,
    badge,
    label,
    legend,
    most_cautious,
    rank,
)


def test_levels_are_ordered_weakest_to_strongest() -> None:
    order = [
        Provenance.FORECAST,
        Provenance.IMPORTED,
        Provenance.POSTED,
        Provenance.RECONCILED,
        Provenance.SEALED,
    ]
    ranks = [rank(lv) for lv in order]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)  # strictly increasing, no ties


def test_most_cautious_returns_the_weakest_input() -> None:
    assert most_cautious([Provenance.SEALED, Provenance.FORECAST]) is Provenance.FORECAST
    assert most_cautious([Provenance.POSTED, Provenance.RECONCILED]) is Provenance.POSTED
    # A summary with no inputs assumes the least certainty, not the most.
    assert most_cautious([]) is Provenance.FORECAST


def test_badge_is_self_contained_html_with_the_label() -> None:
    html = badge(Provenance.FORECAST)
    assert "Forecast" in html
    assert html.startswith("<span") and html.endswith("</span>")
    assert "title=" in html  # carries the explanatory tooltip
    # No external assets — styling is inline, theme-token driven.
    assert "http" not in html and "src=" not in html


def test_legend_explains_the_requested_levels_in_order() -> None:
    leg = legend((Provenance.POSTED, Provenance.SEALED, Provenance.RECONCILED))
    # Rendered weakest → strongest regardless of input order.
    assert leg.index(label(Provenance.POSTED)) < leg.index(label(Provenance.RECONCILED))
    assert leg.index(label(Provenance.RECONCILED)) < leg.index(label(Provenance.SEALED))


def _statements() -> dict[str, object]:
    return {
        "currency": "USD",
        "income_statement": {
            "revenue": [{"code": "4000", "name": "Sales", "amount": "100000"}],
            "expenses": [],
            "total_revenue": "100000", "total_expenses": "0", "net_income": "100000",
        },
        "balance_sheet": {
            "balanced": True, "assets": [], "liabilities": [], "equity": [],
            "total_assets": "100000", "total_liabilities": "0", "total_equity": "100000",
        },
    }


# Only the CARD badges carry a `title=` tooltip attribute (legend badges don't),
# and each level's tooltip text is unique — so these strings identify a card badge.
_POSTED_CARD = 'title="Recorded in the ledger'
_SEALED_CARD = 'title="In a published'


def test_statements_render_tags_figures_posted_by_default() -> None:
    html = render_books_statements("2026-08-01 to 2026-08-31", _statements())
    assert "What do the labels mean?" in html          # the legend is present
    # Both statement cards carry a POSTED badge; none reads SEALED.
    assert html.count(_POSTED_CARD) == 2
    assert _SEALED_CARD not in html


def test_statements_render_tags_figures_sealed_when_period_is_sealed() -> None:
    html = render_books_statements("2026-08-01 to 2026-08-31", _statements(), sealed=True)
    # Both statement cards now read SEALED (final); no card badge reads POSTED.
    assert html.count(_SEALED_CARD) == 2
    assert _POSTED_CARD not in html
