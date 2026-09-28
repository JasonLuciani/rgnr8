"""What the owner should deal with next, in one ranked list.

The product has five separate things that ask for attention, and nothing joins
them up: a floor breach in the forecast, bank lines waiting to be reviewed,
overdue receivables, close tasks that are late or blocked, and warnings that the
books themselves could not be read. Each lives on its own screen. An owner who
wants to know "what needs me today" has to visit five of them and hold the
answer in their head.

This module is the join. It is deliberately **pure policy**: it takes a
`Signals` record of plain values, applies a ranking, and returns items. It does
no I/O, holds no clock, and imports nothing from the screen renderers — so the
ordering rules below can be argued with, and tested, without standing up an app.

Two rules decide the ranking, and both are product judgements worth stating:

1. **Anything that makes the numbers untrustworthy outranks anything that is
   merely due.** A bill paid late costs a late fee. A decision made on figures
   that silently excluded half the bank accounts can cost the business. So
   "we couldn't read your books" sits above "three invoices are overdue", even
   though the second one has a dollar figure attached and the first doesn't.

2. **Not knowing is not the same as nothing.** If the bank feed isn't wired,
   the queue says so. It never renders an empty list that an owner could read
   as "you're all clear" when the truth is "nobody looked." This is the same
   principle the audit asks for in the confidence ladder — never show `$0` for
   unknown — applied to work instead of to money.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rgnr8_forecast import Money
from rgnr8_forecast.brand import format_money

__all__ = ["Item", "Signals", "gather"]


# Ranking bands. An item's urgency is its band plus a within-band adjustment,
# so an item can never jump a band no matter how large its number is -- which
# is the whole point of rule 1 above.
_TRUST = 900   # the figures may be wrong or incomplete
_MONEY = 600   # cash itself is at risk
_WORK = 300    # something is due


@dataclass(frozen=True, slots=True)
class Item:
    """One thing asking for a decision.

    `kind` is the band it was ranked in ("trust" | "money" | "work"), kept on
    the item so a screen can group or filter without re-deriving the policy.
    """

    key: str
    headline: str
    detail: str
    href: str
    action: str
    urgency: int
    kind: str


@dataclass(frozen=True, slots=True)
class Signals:
    """Everything the queue is allowed to know, and nothing else.

    Every field that could be "we don't know" is `None` rather than `0`, and the
    difference is load-bearing: `review_count=0` means someone looked and the
    bank feed is clear; `review_count=None` means no feed is wired and the queue
    must say so instead of implying all-clear.
    """

    tenant: str

    # Forecast / cash
    floor_breached: bool = False
    breach_weeks_until: int | None = None
    breach_shortfall: Money | None = None
    primary_action: str = ""

    # Trust in the numbers
    ledger_problems: Sequence[str] = ()
    data_quality: Sequence[str] = ()
    books_connected: bool = True

    # Bank feed
    review_count: int | None = None

    # Receivables
    overdue_count: int = 0
    overdue_total: Money | None = None
    worst_customer: str = ""
    worst_days: int = 0

    # Month-end close
    close_period: str = ""
    close_overdue: int = 0
    close_blocked: int = 0


def _t(signals: Signals, suffix: str) -> str:
    return f"/t/{signals.tenant}/{suffix}".rstrip("/")


def gather(s: Signals) -> tuple[Item, ...]:
    """The ranked queue for this tenant, most urgent first.

    Returns an empty tuple only when every source was actually checked and every
    one of them was clear. A source that could not be checked produces an item
    saying so, which is why `render`-side code can treat "empty" as genuinely
    good news.
    """
    items: list[Item] = []

    # --- trust band ---------------------------------------------------------
    # The books could not be read. Everything downstream is suspect, including
    # the cash figure at the top of this very screen.
    if s.ledger_problems:
        first = s.ledger_problems[0]
        more = f" (+{len(s.ledger_problems) - 1} more)" if len(s.ledger_problems) > 1 else ""
        items.append(Item(
            key="ledger-problems",
            headline="Your books could not be read in full",
            detail=f"{first}{more}. Figures on this page may be incomplete.",
            href=_t(s, "connect"),
            action="Check the connection",
            urgency=_TRUST + 50,
            kind="trust",
        ))

    if not s.books_connected:
        items.append(Item(
            key="no-books",
            headline="No accounting source is connected",
            detail="Cash and the 13-week outlook are running on assumptions alone "
                   "until a source of record is connected.",
            href=_t(s, "connect"),
            action="Connect QuickBooks",
            urgency=_TRUST + 40,
            kind="trust",
        ))

    # Not knowing is not the same as nothing. See rule 2.
    if s.review_count is None:
        items.append(Item(
            key="feed-unknown",
            headline="No bank feed is wired, so nothing is being reviewed",
            detail="Transactions can't be matched to the books until a feed lands, "
                   "and an unreviewed line is a line the forecast can't see.",
            href=_t(s, "connect"),
            action="Connect a bank",
            urgency=_TRUST + 30,
            kind="trust",
        ))

    for i, note in enumerate(s.data_quality[:3]):
        items.append(Item(
            key=f"data-quality-{i}",
            headline="Worth a second look at the numbers",
            detail=note,
            href=_t(s, "briefing"),
            action="Open the briefing",
            urgency=_TRUST + 10 - i,
            kind="trust",
        ))

    # --- money band ---------------------------------------------------------
    # A floor breach is the single most consequential thing this product can
    # tell an owner, so it leads the band and gets more urgent as it nears.
    if s.floor_breached:
        weeks = s.breach_weeks_until
        when = (
            "This week" if weeks is not None and weeks <= 0
            else f"In {weeks} week{'s' if weeks != 1 else ''}" if weeks is not None
            else "At some point in the next 13 weeks"
        )
        short = f" — short by {format_money(s.breach_shortfall)}" if s.breach_shortfall else ""
        items.append(Item(
            key="floor-breach",
            headline=f"{when}, cash drops below your floor{short}",
            detail=s.primary_action or "Open the outlook to see which week and what's driving it.",
            href=_t(s, ""),
            action="See the outlook",
            # Nearer breaches rank higher, but a breach can never outrank a
            # reason to distrust the forecast that predicted it.
            urgency=_MONEY + (99 - min(99, max(0, weeks if weeks is not None else 13))),
            kind="money",
        ))
    elif s.primary_action:
        items.append(Item(
            key="primary-action",
            headline=s.primary_action,
            detail="This week's single most useful move, from the briefing.",
            href=_t(s, "briefing"),
            action="Open the briefing",
            urgency=_MONEY,
            kind="money",
        ))

    if s.overdue_count:
        who = f"{s.worst_customer} is {s.worst_days} days late" if s.worst_customer else ""
        total = f"{format_money(s.overdue_total)} overdue" if s.overdue_total else ""
        detail = " · ".join(x for x in (total, who) if x) or "Chase the oldest first."
        items.append(Item(
            key="overdue-ar",
            headline=f"{s.overdue_count} invoice{'s' if s.overdue_count != 1 else ''} overdue",
            detail=detail,
            href=_t(s, "receivables"),
            action="Chase them",
            # Scaled by how many, capped well inside the band.
            urgency=_MONEY - 100 + min(99, s.overdue_count * 5),
            kind="money",
        ))

    # --- work band ----------------------------------------------------------
    if s.review_count:
        items.append(Item(
            key="review-queue",
            headline=f"{s.review_count} bank line{'s' if s.review_count != 1 else ''} "
                     "waiting to be reviewed",
            detail="Until these are categorised they aren't in the books, and the "
                   "outlook is working without them.",
            href=_t(s, "inbox"),
            action="Review them",
            urgency=_WORK + min(99, s.review_count),
            kind="work",
        ))

    if s.close_blocked:
        items.append(Item(
            key="close-blocked",
            headline=f"{s.close_blocked} close task{'s' if s.close_blocked != 1 else ''} blocked",
            detail=f"The {s.close_period} close can't finish until these clear.".strip(),
            href=_t(s, "close"),
            action="Open the close",
            urgency=_WORK + 50,
            kind="work",
        ))

    if s.close_overdue:
        items.append(Item(
            key="close-overdue",
            headline=f"{s.close_overdue} close task{'s' if s.close_overdue != 1 else ''} overdue",
            detail=f"{s.close_period} month-end.".strip(),
            href=_t(s, "close"),
            action="Open the close",
            urgency=_WORK + 20,
            kind="work",
        ))

    # Sort by urgency, then by key so equal-urgency items never shuffle between
    # two renders of the same data — a list that reorders itself is a list an
    # owner stops trusting.
    items.sort(key=lambda i: (-i.urgency, i.key))
    return tuple(items)
