"""The operator console — the RGNR8-staff surface for running the beta fleet.

`render_ops_html` is the read-only fleet dashboard; the console wraps that same
worst-first fleet view in operator chrome and adds the two things an operator
actually *does*: onboard a new business, and drill into any client. It reads from
the same `OpsReport` (so the numbers match the dashboard) and stays self-contained
— the onboarding form posts a `forecast-inputs/1` DTO to the operator API, which
lands on `Fleet.onboard_from_dto`, the one provisioning seam both onboarding
stages share.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rgnr8_forecast.brand import POSITIVE, RG_BASE_CSS, RG_TOKENS_CSS, RISK, WATCH, mark_svg

from .report import OpsReport, TenantOpsRow

_STATUS_COLOR = {"AT_RISK": RISK, "WATCH": WATCH, "STABLE": POSITIVE}


def _esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_RECON_COLOR = {"major": RISK, "minor": WATCH, "in_sync": POSITIVE}


def _trust_cell(r: TenantOpsRow) -> str:
    """The trust column: in-sync, drift (with severity + amount), or — when unset."""
    if r.recon_severity is None:
        return '<span class="muted">—</span>'
    color = _RECON_COLOR.get(r.recon_severity, "#888")
    if r.recon_in_sync:
        return f'<span class="dot" style="background:{color}"></span>in sync'
    return (
        f'<span class="dot" style="background:{color}"></span>'
        f'{_esc(r.recon_severity)} drift · {_esc(r.recon_worst_gap)}'
        f'<br><span class="muted">{_esc(r.recon_note)}</span>'
    )


def _row(r: TenantOpsRow, live: "frozenset[str]" = frozenset()) -> str:
    breach = f"week {r.weeks_until_breach} · short {_esc(r.shortfall)}" if r.breached else "—"
    live_badge = (
        ' <span class="rg-live" title="RGNR8 is the system of record">● LIVE</span>'
        if r.tenant_id in live else ""
    )
    if r.books_current is None:
        books = '<span class="muted">—</span>'
    else:
        books = f'<span class="dot" style="background:{POSITIVE if r.books_current else RISK}"></span>{_esc(r.connector_summary)}'
    if r.close_complete is None:
        close = '<span class="muted">—</span>'
    else:
        close = f'<span class="dot" style="background:{POSITIVE if r.close_complete else WATCH}"></span>{_esc(r.close_summary)}'
    deliv = "✓ this week" if r.delivered_this_period else (
        f"last {_esc(r.last_delivered)}" if r.last_delivered else "not yet")
    return f"""<tr>
      <td><strong><a href="/operator/tenant/{_esc(r.tenant_id)}" style="color:var(--rg-ink)">{_esc(r.name)}</a></strong>{live_badge}<br><span class="muted">{_esc(r.recipient)}</span></td>
      <td><span class="dot" style="background:{_STATUS_COLOR.get(r.status, '#888')}"></span>{_esc(r.status)}</td>
      <td class="num">{_esc(r.cash_today)}</td>
      <td class="num">{_esc(r.floor)}</td>
      <td class="num">{_esc(r.trough)}<br><span class="muted">{_esc(r.trough_date)}</span></td>
      <td>{breach}</td><td>{books}</td><td>{close}</td><td>{_trust_cell(r)}</td>
      <td>{deliv}<br><span class="muted">next {_esc(r.next_due[:16])}</span></td>
    </tr>"""


def render_operator_console(
    report: OpsReport,
    *,
    operator: str = "operator@rgnr8.co",
    onboard_action: str = "/operator/onboard",
    coa_templates: "list[dict[str, str]] | None" = None,
    live_tenants: "frozenset[str] | None" = None,
    notice: str = "",
    notice_kind: str = "ok",
) -> str:
    """The full operator console page: forest chrome + operator identity, a fleet
    health banner, the onboarding panel (with a COA-template picker), and the
    worst-first fleet table (with a system-of-record 'live' badge). `notice` shows
    a one-off flash (e.g. after onboarding); `notice_kind` is 'ok' or 'err'."""
    live = live_tenants or frozenset()
    # A one-off flash from the last action (onboard succeeded / failed).
    if notice:
        col = ("#1f6f43", "#EAF7EE", "#BFE3C9") if notice_kind == "ok" else ("#B02A2F", "#FDECEC", "#F5C4C6")
        flash = (f'<div class="card" style="border-color:{col[2]};background:{col[1]};'
                 f'color:{col[0]};margin-bottom:14px">{_esc(notice)}</div>')
    else:
        flash = ""
    # A ready-to-submit default DTO so onboarding works out of the box: today's
    # date, zero opening cash. `available` is minor units (cents) — 0 = $0.00,
    # 9000000 = $90,000.00. Edit it, or connect a live source to fill it.
    as_of = report.as_of[:10] if getattr(report, "as_of", "") else "2026-01-01"
    default_dto = _esc('{"opening": {"as_of": "' + as_of
                       + '", "available": {"minor": 0, "currency": "USD"}, "verified": false}}')
    rows = "\n".join(_row(r, live) for r in report.rows)
    # COA template <select> for onboarding — the chart seeded for the new client.
    if coa_templates:
        opts = "".join(
            f'<option value="{_esc(t["slug"])}">{_esc(t["label"])}</option>' for t in coa_templates
        )
        coa_field = (
            '<label>Chart of accounts (business type)</label>'
            f'<select name="coa_category"><option value="">— none (start empty) —</option>{opts}</select>'
        )
    else:
        coa_field = ""
    banner_cls = "warn" if (report.at_risk or report.books_not_current) else "good"
    if report.at_risk:
        banner = f"{report.at_risk} of {report.total} client(s) AT RISK"
        if report.books_not_current:
            banner += f" · {report.books_not_current} with books behind"
    elif report.books_not_current:
        banner = f"{report.books_not_current} of {report.total} client(s) have books behind"
    else:
        banner = f"All {report.total} client(s) steady"
    empty = '<tr><td colspan="10" class="muted" style="text-align:center;padding:26px">No clients onboarded yet — add the first one above.</td></tr>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 — operator console</title>
<style>
{RG_TOKENS_CSS}
{RG_BASE_CSS}
  .rg-rolechip{{background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.16);border-radius:999px;
    padding:3px 10px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-sage)}}
  .rg-who{{display:flex;align-items:center;gap:10px;color:#DDE6DD;font-size:13px}}
  .wrap{{max-width:1120px;margin:0 auto;padding:24px 20px 56px}}
  h1{{font-size:22px;margin:0 0 2px;font-weight:800;letter-spacing:-.01em}}
  h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--rg-muted);margin:0 0 12px}}
  .sub{{color:var(--rg-muted);margin:0 0 14px;font-size:13px}}
  label{{display:block;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--rg-muted);margin:12px 0 6px}}
  input,select,textarea{{width:100%;padding:11px 12px;border:1px solid var(--rg-line);border-radius:10px;font:inherit;background:#fff;color:var(--rg-ink)}}
  .rg-live{{color:var(--rg-sage);font-size:10px;font-weight:800;letter-spacing:.06em}}
  textarea{{min-height:82px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}}
  .grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}}
  .btn{{padding:11px 16px;margin-top:14px}}
  th,td{{vertical-align:top}}
  .num{{font-weight:700}}
  .muted{{font-size:12px}}
  .note{{color:var(--rg-muted);font-size:12px;margin-top:8px}}
  @media(max-width:640px){{.grid{{grid-template-columns:1fr}}}}
</style></head>
<body>
<header class="rg-bar">
  <span class="rg-lockup">{mark_svg(22, "#F2EFE6")}<span class="rg-wordmark">RGNR<span class="rg-8">8</span></span></span>
  <span class="rg-who"><span>{_esc(operator)}</span><span class="rg-rolechip">Operator</span><a href="/operator/logout" style="color:#DDE6DD;text-decoration:underline;font-size:12px">Sign out</a></span>
</header>
<div class="wrap">
  <h1>Operator console</h1>
  <p class="sub">as of {_esc(report.as_of[:16])} · {report.delivered}/{report.total} briefed this week · {report.closes_done}/{report.total} closed</p>
  <div class="banner {banner_cls}">{banner}</div>
  {flash}
  <div class="card">
    <h2>Onboard a client</h2>
    <form method="post" action="{_esc(onboard_action)}">
      <div class="grid">
        <div><label>Business name</label><input name="name" placeholder="Northwind LLC"></div>
        <div><label>Briefing recipient</label><input name="recipient" placeholder="owner@northwind.com"></div>
        <div><label>Minimum-cash floor (USD)</label><input name="minimum_cash" placeholder="25000.00"></div>
      </div>
      {coa_field}
      <label>forecast-inputs/1 DTO</label>
      <textarea name="dto">{default_dto}</textarea>
      <p class="note">Both onboarding paths land here: the <strong>overlay</strong> (bank balances + open AR/AP on top of QBO) and the full <strong>migration</strong> (QBO ledger imported into RGNR8) each emit this same DTO. Connect a live source at onboarding to fill it automatically.</p>
      <button class="btn" type="submit">Onboard client</button>
    </form>
  </div>

  <h2>Fleet — worst first</h2>
  <div class="table-scroll"><table>
    <thead><tr><th>Client</th><th>Status</th><th class="num">Cash</th><th class="num">Floor</th><th class="num">Trough</th><th>Breach</th><th>Books</th><th>Close</th><th>Trust</th><th>Briefing</th></tr></thead>
    <tbody>
{rows or empty}
    </tbody>
  </table></div>
</div>
</body></html>"""


_DETAIL_STYLE = f"""<style>
{RG_TOKENS_CSS}
{RG_BASE_CSS}
  .rg-rolechip{{background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.16);border-radius:999px;
    padding:3px 10px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-sage)}}
  .rg-who{{display:flex;align-items:center;gap:10px;color:#DDE6DD;font-size:13px}}
  .wrap{{max-width:1120px;margin:0 auto;padding:24px 20px 56px}}
  h1{{font-size:22px;margin:0 0 2px;font-weight:800;letter-spacing:-.01em}}
  h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--rg-muted);margin:0 0 12px}}
  .sub{{color:var(--rg-muted);margin:0 0 16px;font-size:13px}}
  label{{display:block;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--rg-muted);margin:12px 0 6px}}
  input,select{{width:100%;padding:11px 12px;border:1px solid var(--rg-line);border-radius:10px;font:inherit;background:#fff;color:var(--rg-ink)}}
  .btn{{padding:11px 16px;margin-top:14px}}
  .cards{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
  table{{width:100%;border-collapse:collapse}}
  th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--rg-line);font-size:14px}}
  th{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--rg-muted)}}
  .num{{font-weight:700}} .muted{{color:var(--rg-muted);font-size:12px}}
  .rg-live{{color:var(--rg-sage);font-size:11px;font-weight:800;letter-spacing:.06em}}
  .grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}}
  @media(max-width:760px){{.cards,.grid{{grid-template-columns:1fr}}}}
</style>"""


def render_client_detail(
    *,
    tenant_id: str,
    name: str,
    operator: str = "operator@rgnr8.co",
    row: "TenantOpsRow | None" = None,
    cutover: "Mapping[str, object] | None" = None,
    members: "Sequence[Mapping[str, object]] | None" = None,
    assignable_roles: "list[str] | None" = None,
    notice: str = "",
    notice_kind: str = "ok",
) -> str:
    """A per-client control page: status snapshot, go-live state + mark-live, and
    team (member/role) management. The forms post form-encoded to the existing
    /operator/tenant/<id>/... routes and redirect back here with a flash."""
    members = members or []
    assignable_roles = assignable_roles or []
    if notice:
        c = ("#1f6f43", "#EAF7EE", "#BFE3C9") if notice_kind == "ok" else ("#B02A2F", "#FDECEC", "#F5C4C6")
        flash = (f'<div class="card" style="border-color:{c[2]};background:{c[1]};color:{c[0]};'
                 f'margin-bottom:14px">{_esc(notice)}</div>')
    else:
        flash = ""

    if row is not None:
        breach = (f"breaches in week {row.weeks_until_breach} · short {_esc(row.shortfall)}"
                  if row.breached else "no covenant breach in the horizon")
        status_card = (
            '<div class="card"><h2>Status</h2>'
            f'<div style="display:flex;align-items:center;gap:8px;font-weight:700;font-size:16px">'
            f'<span class="dot" style="background:{_STATUS_COLOR.get(row.status, "#888")}"></span>{_esc(row.status)}</div>'
            '<table style="margin-top:10px">'
            f'<tr><td>Cash today</td><td class="num">{_esc(row.cash_today)}</td></tr>'
            f'<tr><td>Floor</td><td class="num">{_esc(row.floor)}</td></tr>'
            f'<tr><td>Trough</td><td class="num">{_esc(row.trough)} <span class="muted">{_esc(row.trough_date)}</span></td></tr>'
            f'<tr><td>Breach</td><td>{_esc(breach)}</td></tr></table></div>'
        )
    else:
        status_card = '<div class="card"><h2>Status</h2><p class="muted">No forecast row yet.</p></div>'

    if cutover:
        golive_card = (
            '<div class="card"><h2>Go-live</h2>'
            '<p><span class="rg-live">● LIVE</span> — RGNR8 is the system of record.</p>'
            '<table>'
            f'<tr><td>Source</td><td>{_esc(cutover.get("source_system"))}</td></tr>'
            f'<tr><td>Cutover date</td><td>{_esc(cutover.get("cutover_date"))}</td></tr>'
            f'<tr><td>Marked by</td><td>{_esc(cutover.get("marked_by"))}</td></tr></table></div>'
        )
    else:
        golive_card = (
            '<div class="card"><h2>Go-live</h2>'
            '<p class="muted">Not live yet — RGNR8 is not the system of record for this client.</p>'
            f'<form method="post" action="/operator/tenant/{_esc(tenant_id)}/cutover">'
            '<label>Source system</label>'
            '<select name="source_system"><option value="quickbooks">QuickBooks</option>'
            '<option value="xero">Xero</option><option value="other">Other</option></select>'
            '<label>Cutover date</label><input name="cutover_date" type="date">'
            '<button class="btn" type="submit">Mark live</button></form></div>'
        )

    role_opts = "".join(f'<option value="{_esc(r)}">{_esc(str(r).capitalize())}</option>'
                        for r in assignable_roles)
    member_rows = "".join(
        f'<tr><td>{_esc(m.get("email") or m.get("user_id") or "")}</td>'
        f'<td>{_esc(m.get("role") or "")}</td></tr>' for m in members
    ) or '<tr><td colspan="2" class="muted">No members yet.</td></tr>'
    team_card = (
        '<div class="card" style="grid-column:1/-1"><h2>Team</h2>'
        f'<table><thead><tr><th>Member</th><th>Role</th></tr></thead><tbody>{member_rows}</tbody></table>'
        f'<form method="post" action="/operator/tenant/{_esc(tenant_id)}/users" style="margin-top:12px">'
        '<div class="grid">'
        '<div><label>Email</label><input name="email" type="email" placeholder="person@company.com"></div>'
        '<div><label>Name (optional)</label><input name="name"></div>'
        f'<div><label>Role</label><select name="role"><option value="">— remove access —</option>{role_opts}</select></div>'
        '</div><button class="btn" type="submit">Add / update member</button></form></div>'
    )

    inner = (f'<p class="sub"><a href="/operator" style="color:var(--rg-forest)">← Back to fleet</a></p>'
             f'<h1>{_esc(name)}</h1><p class="sub">{_esc(tenant_id)}</p>{flash}'
             f'<div class="cards">{status_card}{golive_card}{team_card}</div>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 — {_esc(name)}</title>
{_DETAIL_STYLE}</head>
<body>
<header class="rg-bar">
  <span class="rg-lockup">{mark_svg(22, "#F2EFE6")}<span class="rg-wordmark">RGNR<span class="rg-8">8</span></span></span>
  <span class="rg-who"><span>{_esc(operator)}</span><span class="rg-rolechip">Operator</span><a href="/operator/logout" style="color:#DDE6DD;text-decoration:underline;font-size:12px">Sign out</a></span>
</header>
<div class="wrap">{inner}</div>
</body></html>"""


def render_operator_login(*, error: str = "") -> str:
    """A minimal, brand-consistent staff login for the operator console. Posts
    email+password to /operator/login, which verifies the credential and (only for
    a platform role) sets a session cookie."""
    err = f'<div class="banner warn">{_esc(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 — operator sign in</title>
<style>
{RG_TOKENS_CSS}
{RG_BASE_CSS}
  .box{{max-width:380px;margin:8vh auto;padding:0 20px}}
  h1{{font-size:20px;margin:0 0 4px;font-weight:800}}
  .sub{{color:var(--rg-muted);margin:0 0 18px;font-size:13px}}
  label{{display:block;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--rg-muted);margin:12px 0 6px}}
  input{{width:100%;padding:11px 12px;border:1px solid var(--rg-line);border-radius:10px;font:inherit;background:#fff;color:var(--rg-ink)}}
  .btn{{padding:11px 16px;margin-top:16px;width:100%}}
</style></head>
<body>
<header class="rg-bar">
  <span class="rg-lockup">{mark_svg(22, "#F2EFE6")}<span class="rg-wordmark">RGNR<span class="rg-8">8</span></span></span>
</header>
<div class="box">
  <div class="card">
    <h1>Operator sign in</h1>
    <p class="sub">RGNR8 staff only. Your account must hold an operator or support role.</p>
    {err}
    <form method="post" action="/operator/login">
      <label>Work email</label><input name="email" type="email" placeholder="you@rgnr8.co" autocomplete="username">
      <label>Password</label><input name="password" type="password" autocomplete="current-password">
      <button class="btn" type="submit">Sign in</button>
    </form>
  </div>
</div>
</body></html>"""
