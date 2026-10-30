"""The business's own calendar: what "today" means for a company.

The server runs in UTC (Render), so `date.today()` turns over at 7pm in
Mountain time and a transaction entered at 11:30pm on the last day of the month
lands in the next month. Business dates (the default date on a form, "as of
today" reports, the current period, aging buckets, overdue checks, go-live and
cutover defaults) are therefore computed here, in the company's time zone.

Technical timestamps (audit `at`, token expiry, webhook and session clocks,
created_at) stay in UTC and do not use this module.

The zone is a per-company setting (`TenantState.timezone`, an IANA name). A
company that never chose one gets `DEFAULT_COMPANY_TIMEZONE`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The one place the default lives. RGNR8 is a one-company deployment today and
# that company is in US Mountain time; change this constant to change the
# default for every company that has not chosen its own zone.
DEFAULT_COMPANY_TIMEZONE = "America/Denver"

# The zones offered in Settings. Free text is accepted too; see `validate_timezone`.
COMMON_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("America/New_York", "Eastern (America/New_York)"),
    ("America/Chicago", "Central (America/Chicago)"),
    ("America/Denver", "Mountain (America/Denver)"),
    ("America/Phoenix", "Arizona, no daylight saving (America/Phoenix)"),
    ("America/Los_Angeles", "Pacific (America/Los_Angeles)"),
    ("America/Anchorage", "Alaska (America/Anchorage)"),
    ("Pacific/Honolulu", "Hawaii (Pacific/Honolulu)"),
    ("UTC", "UTC"),
)


def validate_timezone(name: str) -> str:
    """Return the cleaned IANA zone name, or raise ValueError for an unknown one."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("choose a time zone")
    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"{cleaned!r} is not a time zone name such as America/Denver") from exc
    return cleaned


def company_zone(tz_name: str | None) -> ZoneInfo:
    """The company's zone. A missing or unreadable stored name falls back to the
    default rather than failing every page; `validate_timezone` stops bad names
    from being saved in the first place."""
    try:
        return ZoneInfo(tz_name or DEFAULT_COMPANY_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(DEFAULT_COMPANY_TIMEZONE)


def company_now(tz_name: str | None, now: datetime | None = None) -> datetime:
    """The instant `now` (default: the wall clock) as wall-clock time in the company's zone."""
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("company_now needs a timezone-aware datetime")
    return instant.astimezone(company_zone(tz_name))


def company_today(tz_name: str | None, now: datetime | None = None) -> date:
    """The company's calendar date at `now` (default: the wall clock)."""
    return company_now(tz_name, now).date()
