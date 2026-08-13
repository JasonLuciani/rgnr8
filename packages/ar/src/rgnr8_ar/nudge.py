"""Collection nudge drafts.

Produces a plain-text reminder for a single invoice, with a tone that escalates by
aging bucket: a friendly reminder while current / recently past due, a firmer note
at 31-60 days, and a final notice past 60. Drafts only — nothing is ever sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from rgnr8_forecast import Invoice

from .aging import AgingBucket, bucket_for, days_overdue


class NudgeTone(str, Enum):
    """Escalating collection tones, ordered FRIENDLY < FIRM < FINAL."""

    FRIENDLY = "FRIENDLY"
    FIRM = "FIRM"
    FINAL = "FINAL"


# Tone per aging bucket — monotonic non-decreasing in age.
_TONE_BY_BUCKET: dict[AgingBucket, NudgeTone] = {
    AgingBucket.CURRENT: NudgeTone.FRIENDLY,
    AgingBucket.D1_30: NudgeTone.FRIENDLY,
    AgingBucket.D31_60: NudgeTone.FIRM,
    AgingBucket.D60_PLUS: NudgeTone.FINAL,
}


@dataclass(frozen=True, slots=True)
class CollectionNudge:
    """A drafted, unsent reminder for one invoice."""

    invoice: Invoice
    tone: NudgeTone
    subject: str
    body: str


def tone_for(bucket: AgingBucket) -> NudgeTone:
    """The collection tone appropriate to an aging bucket."""
    return _TONE_BY_BUCKET[bucket]


def draft_nudge(invoice: Invoice, as_of: date) -> CollectionNudge:
    """Draft a plain-text collection nudge for ``invoice`` as of ``as_of``."""
    bucket = bucket_for(invoice.due_date, as_of)
    tone = tone_for(bucket)
    overdue = days_overdue(invoice.due_date, as_of)
    amount = invoice.open_amount
    due = invoice.due_date.isoformat()

    if tone is NudgeTone.FRIENDLY:
        if overdue <= 0:
            subject = f"Friendly reminder: invoice {invoice.id} due {due}"
            body = (
                f"Hi,\n\n"
                f"Just a friendly reminder that invoice {invoice.id} for {amount} "
                f"is due on {due}. If it is already on its way, thank you and "
                f"please disregard this note.\n\n"
                f"Thanks so much,\nAccounts Receivable"
            )
        else:
            subject = f"Quick reminder: invoice {invoice.id} is {overdue} day(s) past due"
            body = (
                f"Hi,\n\n"
                f"We wanted to flag that invoice {invoice.id} for {amount} was due "
                f"on {due} and is now {overdue} day(s) past due. A quick payment "
                f"when you have a moment would be much appreciated.\n\n"
                f"Thanks,\nAccounts Receivable"
            )
    elif tone is NudgeTone.FIRM:
        subject = f"Past due: invoice {invoice.id} is {overdue} day(s) overdue"
        body = (
            f"Hello,\n\n"
            f"Our records show invoice {invoice.id} for {amount}, due {due}, is now "
            f"{overdue} day(s) overdue. Please arrange payment promptly, or reply so "
            f"we can sort out anything holding it up.\n\n"
            f"Regards,\nAccounts Receivable"
        )
    else:  # NudgeTone.FINAL
        subject = f"FINAL NOTICE: invoice {invoice.id} is {overdue} day(s) overdue"
        body = (
            f"Hello,\n\n"
            f"This is a final notice. Invoice {invoice.id} for {amount}, due {due}, "
            f"is now {overdue} day(s) overdue and requires immediate payment. Please "
            f"remit the full balance or contact us right away to avoid further "
            f"collection steps.\n\n"
            f"Accounts Receivable"
        )

    return CollectionNudge(invoice=invoice, tone=tone, subject=subject, body=body)
