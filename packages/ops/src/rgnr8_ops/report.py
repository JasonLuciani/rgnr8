"""The operator ops report — watch the whole beta fleet from one view.

For each onboarded client it runs the forecast, reads the briefing status
(STABLE / WATCH / AT_RISK), and pulls the delivery cursor, so an operator can see
at a glance: whose cash is at risk, how soon each breaches its floor, and whether
this week's briefing has gone out / when it's next due. Deterministic — `now` is
injected; the report renders to a self-contained HTML dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rgnr8_briefing import Subscription, build_briefing, most_recent_fire, next_fire
from rgnr8_forecast import run_forecast
from rgnr8_forecast.brand import POSITIVE, RG_BASE_CSS, RG_TOKENS_CSS, RISK, WATCH, brand_bar
from rgnr8_recon_monitor import Severity as ReconSeverity

from .fleet import Fleet


@dataclass(frozen=True, slots=True)
class TenantOpsRow:
    tenant_id: str
    name: str
    recipient: str
    status: str  # STABLE | WATCH | AT_RISK
    cash_today: str
    floor: str
    trough: str
    trough_date: str
    breached: bool
    weeks_until_breach: int | None
    shortfall: str
    headline: str
    last_delivered: str | None  # ISO of the last fire satisfied, or None
    delivered_this_period: bool
    next_due: str  # ISO datetime of the next scheduled briefing
    # operational status (None when no connector/close status is attached)
    books_current: bool | None
    connector_summary: str
    close_complete: bool | None
    close_summary: str
    # trust / reconciliation (None severity when no recon figures are attached)
    recon_severity: str | None  # "in_sync" | "minor" | "major" | None
    recon_in_sync: bool | None
    recon_note: str
    recon_worst_gap: str
    recon_ledger_vs_bank: str


@dataclass(frozen=True, slots=True)
class OpsReport:
    as_of: str
    rows: tuple[TenantOpsRow, ...]

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def at_risk(self) -> int:
        return sum(1 for r in self.rows if r.status == "AT_RISK")

    @property
    def delivered(self) -> int:
        return sum(1 for r in self.rows if r.delivered_this_period)

    @property
    def undelivered(self) -> int:
        return self.total - self.delivered

    @property
    def books_not_current(self) -> int:
        """Clients whose connectors need attention or have gone stale."""
        return sum(1 for r in self.rows if r.books_current is False)

    @property
    def closes_done(self) -> int:
        return sum(1 for r in self.rows if r.close_complete is True)

    @property
    def recon_drift(self) -> int:
        """Clients whose ledger/bank/forecast figures diverge (attached + not in sync)."""
        return sum(1 for r in self.rows if r.recon_severity not in (None, "in_sync"))


def build_ops_report(fleet: Fleet, now: datetime) -> OpsReport:
    subs: dict[str, Subscription] = {s.tenant_id: s for s in fleet.subscriptions.list()}
    rows: list[TenantOpsRow] = []
    for bt in fleet.tenants.values():
        fc = run_forecast(bt.inputs, bt.config)
        b = build_briefing(fc)
        p = fc.projection
        sub = subs.get(bt.tenant_id)

        last_delivered: str | None = None
        delivered_this_period = False
        next_due_iso = ""
        if sub is not None:
            fire = most_recent_fire(sub.schedule, now)
            if sub.last_sent is not None:
                last_delivered = sub.last_sent.isoformat()
                delivered_this_period = sub.last_sent >= fire
            next_due_iso = next_fire(sub.schedule, now).isoformat()

        st = fleet.statuses.get(bt.tenant_id)
        books_current: bool | None = None
        connector_summary = "—"
        close_complete: bool | None = None
        close_summary = "—"
        if st is not None:
            if st.connectors is not None:
                books_current = st.connectors.books_current
                connector_summary = st.connectors.summary()
            if st.close is not None:
                close_complete = st.close.complete
                close_summary = st.close.summary()

        recon_severity: str | None = None
        recon_in_sync: bool | None = None
        recon_note = "—"
        recon_worst_gap = "—"
        recon_ledger_vs_bank = "—"
        rc = fleet.recon.get(bt.tenant_id)
        if rc is not None:
            div = rc.divergence(bt.tenant_id, now.date())
            recon_severity = div.severity.value
            recon_in_sync = div.severity is ReconSeverity.IN_SYNC
            recon_note = div.note
            recon_worst_gap = div.worst_gap.to_decimal_string()
            recon_ledger_vs_bank = div.ledger_vs_bank.to_decimal_string()

        rows.append(
            TenantOpsRow(
                tenant_id=bt.tenant_id,
                name=bt.name,
                recipient=bt.recipient,
                status=b.status.value,
                cash_today=p.opening_available.to_decimal_string(),
                floor=p.effective_floor.to_decimal_string(),
                trough=p.trough.balance.to_decimal_string(),
                trough_date=p.trough.on_date.isoformat(),
                breached=p.breach.breached,
                weeks_until_breach=p.breach.weeks_until,
                shortfall=p.breach.worst_shortfall.to_decimal_string(),
                headline=b.headline,
                last_delivered=last_delivered,
                delivered_this_period=delivered_this_period,
                next_due=next_due_iso,
                books_current=books_current,
                connector_summary=connector_summary,
                close_complete=close_complete,
                close_summary=close_summary,
                recon_severity=recon_severity,
                recon_in_sync=recon_in_sync,
                recon_note=recon_note,
                recon_worst_gap=recon_worst_gap,
                recon_ledger_vs_bank=recon_ledger_vs_bank,
            )
        )
    # sort worst-first: AT_RISK, then WATCH, then STABLE; within, soonest breach
    order = {"AT_RISK": 0, "WATCH": 1, "STABLE": 2}
    rows.sort(key=lambda r: (order.get(r.status, 3), r.weeks_until_breach if r.weeks_until_breach is not None else 99))
    return OpsReport(as_of=now.isoformat(), rows=tuple(rows))


# --- HTML dashboard ----------------------------------------------------------

_STATUS_COLOR = {"AT_RISK": RISK, "WATCH": WATCH, "STABLE": POSITIVE}


def _esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_ops_html(report: OpsReport) -> str:
    def row_html(r: TenantOpsRow) -> str:
        breach = (
            f"week {r.weeks_until_breach} · short {_esc(r.shortfall)}"
            if r.breached
            else "—"
        )
        deliv = (
            "✓ this week"
            if r.delivered_this_period
            else (f"last {_esc(r.last_delivered)}" if r.last_delivered else "not yet")
        )
        if r.books_current is None:
            books = '<span class="muted">—</span>'
        else:
            bdot = POSITIVE if r.books_current else RISK
            books = f'<span class="dot" style="background:{bdot}"></span>{_esc(r.connector_summary)}'
        if r.close_complete is None:
            close = '<span class="muted">—</span>'
        else:
            cdot = POSITIVE if r.close_complete else WATCH
            close = f'<span class="dot" style="background:{cdot}"></span>{_esc(r.close_summary)}'
        return f"""<tr>
        <td><strong>{_esc(r.name)}</strong><br><span class="muted">{_esc(r.recipient)}</span></td>
        <td><span class="dot" style="background:{_STATUS_COLOR.get(r.status, '#888')}"></span>{_esc(r.status)}</td>
        <td class="num">{_esc(r.cash_today)}</td>
        <td class="num">{_esc(r.floor)}</td>
        <td class="num">{_esc(r.trough)}<br><span class="muted">{_esc(r.trough_date)}</span></td>
        <td>{breach}</td>
        <td>{books}</td>
        <td>{close}</td>
        <td>{deliv}<br><span class="muted">next {_esc(r.next_due[:16])}</span></td>
      </tr>"""

    rows = "\n".join(row_html(r) for r in report.rows)
    banner_cls = "warn" if (report.at_risk or report.books_not_current) else "good"
    if report.at_risk:
        banner = f"{report.at_risk} of {report.total} client(s) AT RISK"
        if report.books_not_current:
            banner += f" · {report.books_not_current} with books behind"
    elif report.books_not_current:
        banner = f"{report.books_not_current} of {report.total} client(s) have books behind (reconnect/stale)"
    else:
        banner = f"All {report.total} client(s) steady"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 — beta fleet</title>
<style>
{RG_TOKENS_CSS}
{RG_BASE_CSS}
  .wrap{{max-width:1080px;margin:0 auto;padding:24px 20px 56px}}
  h1{{font-size:22px;margin:0 0 2px;font-weight:800;letter-spacing:-.01em}}
  .sub{{color:var(--rg-muted);margin:0 0 14px;font-size:13px}}
  th,td{{vertical-align:top}}
  .num{{font-weight:700}}
  .muted{{font-size:12px}}
</style></head>
<body>
{brand_bar("Beta fleet")}
<div class="wrap">
  <h1>Beta fleet</h1>
  <p class="sub">as of {_esc(report.as_of[:16])} · {report.delivered}/{report.total} delivered this week · {report.closes_done}/{report.total} closed</p>
  <div class="banner {banner_cls}">{banner}</div>
  <div class="table-scroll"><table>
    <thead><tr><th>Client</th><th>Status</th><th class="num">Cash</th><th class="num">Floor</th><th class="num">Trough</th><th>Breach</th><th>Books</th><th>Close</th><th>Briefing</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table></div>
</div>
</body></html>"""
