"""Business dates follow the company's time zone, not the server's (UTC).

The owner is in US Mountain time. At 23:30 on 2026-10-31 in Denver it is already
05:30 UTC on 2026-11-01, so a server-UTC "today" puts an entry made that evening
into November. The company's own calendar says October.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from test_go_live_readiness import _MigratedFromQuickBooks

from factory import _inputs, usd
from rgnr8_forecast import ForecastConfig
from rgnr8_web import LedgerClient, Request, WebApp
from rgnr8_web.company_time import (
    COMMON_TIMEZONES,
    DEFAULT_COMPANY_TIMEZONE,
    company_now,
    company_today,
    validate_timezone,
)
from rgnr8_web.store import InMemoryTenantStore, TenantState

# 2026-10-31 23:30 America/Denver is 2026-11-01 05:30 UTC.
INSTANT = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
OCT_31 = date(2026, 10, 31)


# --- the helpers --------------------------------------------------------------


def test_the_default_zone_is_mountain_time() -> None:
    assert DEFAULT_COMPANY_TIMEZONE == "America/Denver"


def test_at_2330_mountain_on_the_last_day_it_is_still_october_31() -> None:
    assert company_today("America/Denver", INSTANT) == OCT_31
    assert company_today(None, INSTANT) == OCT_31  # no setting: the default zone


def test_a_tenant_in_tokyo_at_the_same_instant_is_already_november_1() -> None:
    assert company_today("Asia/Tokyo", INSTANT) == date(2026, 11, 1)


def test_company_now_is_wall_clock_time_in_the_zone() -> None:
    local = company_now("America/Denver", INSTANT)
    assert (local.hour, local.minute, local.day, local.month) == (23, 30, 31, 10)
    assert local.utcoffset().total_seconds() == -6 * 3600  # MDT, daylight saving


def test_a_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError):
        company_now("America/Denver", datetime(2026, 11, 1, 5, 30))


@pytest.mark.parametrize("name", ["Mars/Olympus", "America/Denvr", "../etc/passwd", "", "   "])
def test_an_unknown_or_empty_zone_name_is_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        validate_timezone(name)


def test_a_real_zone_is_accepted_and_trimmed() -> None:
    assert validate_timezone("  Europe/Dublin ") == "Europe/Dublin"


def test_every_zone_offered_in_settings_is_valid() -> None:
    for name, _label in COMMON_TIMEZONES:
        assert validate_timezone(name) == name


# --- the books and the period are read in the company's zone -------------------


def _app(ledger, *, state: TenantState | None = None, clock=lambda: INSTANT) -> WebApp:
    store = InMemoryTenantStore()
    if state is not None:
        store.save("acme", state)
    app = WebApp(business_clock=clock, store=store)
    app.add_tenant("acme", "Acme Co", _inputs("20000.00"),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="tok-acme")
    app.set_ledger(LedgerClient(ledger, token="t"))
    return app


def _get(app, path: str) -> str:
    return app.handle(Request("GET", path, {"authorization": "Bearer tok-acme"}, "")).body


def _trial_balance_dates(ledger: _MigratedFromQuickBooks) -> set[str]:
    out = set()
    for p in ledger.paths:
        if "/trial-balance" in p:
            out.add(parse_qs(urlsplit(p).query)["to"][0])
    return out


def test_at_2330_mountain_the_books_are_read_as_of_october_31() -> None:
    ledger = _MigratedFromQuickBooks()
    body = _get(_app(ledger), "/t/acme")
    assert _trial_balance_dates(ledger) == {"2026-10-31"}
    assert "as of 2026-10-31" in body


def test_the_current_period_is_october_not_november() -> None:
    app = _app(_MigratedFromQuickBooks())
    assert app._today(app._tenants["acme"]) == "2026-10-31"  # noqa: SLF001
    assert app._today(app._tenants["acme"])[:7] == "2026-10"  # noqa: SLF001


def test_a_tenant_set_to_tokyo_is_already_november_1() -> None:
    ledger = _MigratedFromQuickBooks()
    _get(_app(ledger, state=TenantState(timezone="Asia/Tokyo")), "/t/acme")
    assert _trial_balance_dates(ledger) == {"2026-11-01"}


def test_the_ledger_is_told_the_company_today_for_overdue_and_aging() -> None:
    ledger = _MigratedFromQuickBooks()
    _get(_app(ledger), "/t/acme/invoices")
    docs = [p for p in ledger.paths if p.split("?")[0].endswith("/invoices")]
    assert docs and all(parse_qs(urlsplit(p).query).get("today") == ["2026-10-31"] for p in docs), docs


def test_with_no_business_clock_the_saved_opening_date_still_anchors_the_books() -> None:
    ledger = _MigratedFromQuickBooks()
    app = WebApp(business_clock=None)
    app.add_tenant("acme", "Acme Co", _inputs("20000.00"),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="tok-acme")
    app.set_ledger(LedgerClient(ledger, token="t"))
    assert app._today(app._tenants["acme"]) == "2026-08-03"  # noqa: SLF001


# --- Settings: the zone is chosen there and validated -------------------------


def _post_settings(app, form: str):
    return app.handle(Request(
        "POST", "/t/acme/settings", {"authorization": "Bearer tok-acme",
                                     "content-type": "application/x-www-form-urlencoded"}, form))


def test_settings_saves_a_chosen_zone_and_the_business_date_follows_it() -> None:
    app = _app(_MigratedFromQuickBooks())
    _post_settings(app, "timezone=Asia%2FTokyo&timezone_other=")
    assert app._store.load("acme").timezone == "Asia/Tokyo"  # noqa: SLF001
    assert app._today(app._tenants["acme"]) == "2026-11-01"  # noqa: SLF001


def test_settings_accepts_a_typed_zone_when_other_is_chosen() -> None:
    app = _app(_MigratedFromQuickBooks())
    _post_settings(app, "timezone=other&timezone_other=Europe%2FDublin")
    assert app._store.load("acme").timezone == "Europe/Dublin"  # noqa: SLF001


def test_settings_rejects_an_unknown_zone_and_saves_nothing() -> None:
    app = _app(_MigratedFromQuickBooks())
    res = _post_settings(app, "timezone=other&timezone_other=Mars%2FOlympus")
    assert res.status == 302
    assert "err=" in dict(res.headers)["Location"]
    assert app._store.load("acme").timezone is None  # noqa: SLF001


def test_a_stored_zone_survives_a_round_trip_through_the_store() -> None:
    assert TenantState.from_dict(TenantState(timezone="America/Denver").to_dict()).timezone == "America/Denver"
    assert TenantState.from_dict({}).timezone is None


def test_the_settings_screen_selects_the_current_zone_or_other_for_a_typed_one() -> None:
    from rgnr8_web.settings_screens import render_settings

    listed = render_settings("acme", {}, can_manage_settings=True, can_manage_retention=False,
                             can_erase=False, timezone_name="America/Denver")
    assert '<option value="America/Denver" selected>' in listed

    typed = render_settings("acme", {}, can_manage_settings=True, can_manage_retention=False,
                            can_erase=False, timezone_name="Europe/Dublin")
    assert '<option value="other" selected>' in typed
    assert 'name="timezone_other" value="Europe/Dublin"' in typed
