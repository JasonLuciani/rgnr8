"""Server-rendered app UI: login, the role-aware app shell, and users admin.

The prototype made real. These are the pages a browser actually loads: a brand
login screen, a persistent **app shell** (forest top bar + wordmark + role-aware
nav + identity/role chip) that wraps each screen's body, and a **users-admin**
page (invite / change role / remove). Everything is self-contained (system fonts,
no external assets — audited by the same test as the other renderers) and reads
from the RBAC model: the nav only shows what the caller's role permits, and the
owner lands on cash by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from html import escape

from rgnr8_forecast.brand import IVORY, RG_BASE_CSS, RG_TOKENS_CSS, mark_svg

from .audit import AuditEvent
from .rbac import Membership, Permission, Role, User

# nav item → (path suffix, label, permission required)
_NAV: list[tuple[str, str, Permission]] = [
    ("", "Cash", Permission.VIEW_CASH),
    ("transactions", "Transactions", Permission.VIEW_TRANSACTIONS),
    ("books", "Books", Permission.VIEW_TRANSACTIONS),
    ("jobs", "Jobs", Permission.VIEW_TRANSACTIONS),
    ("estimates", "Estimates", Permission.VIEW_TRANSACTIONS),
    ("pipeline", "Pipeline", Permission.VIEW_TRANSACTIONS),
    ("invoices", "Invoices", Permission.VIEW_TRANSACTIONS),
    ("bills", "Bills", Permission.VIEW_TRANSACTIONS),
    ("payroll", "Payroll", Permission.VIEW_TRANSACTIONS),
    ("scenarios", "Scenarios", Permission.VIEW_CASH),
    ("receivables", "Receivables", Permission.VIEW_CASH),
    ("reports", "Reports", Permission.VIEW_CASH),
    ("briefing", "Briefing", Permission.VIEW_BRIEFING),
    ("close", "Close", Permission.MANAGE_CLOSE),
    ("packages", "Package", Permission.VIEW_PACKAGE),
    ("connect", "Connect", Permission.MANAGE_CONNECTORS),
    ("team", "Team", Permission.MANAGE_USERS),
    ("settings", "Settings", Permission.MANAGE_SETTINGS),
    ("integrations", "Integrations", Permission.MANAGE_INTEGRATIONS),
    ("audit", "Audit", Permission.MANAGE_USERS),
]

_SHELL_CSS = f"""<style>
{RG_TOKENS_CSS}
{RG_BASE_CSS}
  .rg-nav{{display:flex;gap:2px;flex-wrap:wrap}}
  .rg-nav a{{color:#CBD8CC;text-decoration:none;padding:8px 12px;border-radius:8px;font-size:13px;font-weight:600;letter-spacing:.02em}}
  .rg-nav a.active,.rg-nav a:hover{{background:rgba(255,255,255,.10);color:#fff}}
  .rg-who{{display:flex;align-items:center;gap:10px;color:#DDE6DD;font-size:13px}}
  .rg-rolechip{{background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.16);border-radius:999px;
    padding:3px 10px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-sage)}}
  .rg-who a{{color:#CBD8CC;text-decoration:none;font-size:12px}}
  .shell{{max-width:1040px;margin:0 auto;padding:24px 20px 60px}}
  h1{{font-size:24px;font-weight:700;letter-spacing:-.01em;margin:0 0 4px}}
  .sub{{color:var(--rg-muted);margin:0 0 16px;font-size:13px}}
  label{{display:block;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--rg-muted);margin:14px 0 6px}}
  input,select{{width:100%;padding:11px 12px;border:1px solid var(--rg-line);border-radius:10px;font:inherit;background:#fff;color:var(--rg-ink)}}
  .row{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}} .grow{{flex:1;min-width:200px}}
  h2{{font-size:15px;font-weight:700;letter-spacing:-.01em;margin:22px 0 10px}}
  .bar{{height:9px;background:var(--rg-ivory);border:1px solid var(--rg-line);border-radius:6px;overflow:hidden}}
  .bar>span{{display:block;height:100%;background:var(--rg-sage)}}
  .drv{{margin-bottom:11px}} .drv .t{{display:flex;justify-content:space-between;font-size:14px}}
  .drv .track{{height:8px;background:var(--rg-ivory);border-radius:5px;margin:5px 0 2px;overflow:hidden}}
  .drv .fill{{height:100%;background:var(--rg-sage);border-radius:5px}} .drv .s{{color:var(--rg-muted);font-size:12px}}
  .chip{{font:inherit;font-size:13px;color:var(--rg-ink);background:var(--rg-ivory);border:1px solid var(--rg-line);border-radius:999px;padding:7px 13px;cursor:pointer}}
  .chip:hover{{border-color:var(--rg-sage);color:var(--rg-forest)}}
  .empty{{text-align:center;padding:32px 24px}}
  @media(max-width:640px){{
    .rg-nav{{display:none}}
    table{{font-size:12.5px}}
    th,td{{padding:9px 10px}}
  }}
</style>"""

_ASSIGNABLE_ROLES = [r for r in Role if not r.is_platform]


def _doc(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)}</title>{_SHELL_CSS}</head><body>{body}</body></html>"
    )


def render_login_html(*, action: str = "/login", sso: bool = False, error: str | None = None) -> str:
    """The brand login screen. `sso=True` shows a 'Sign in with SSO' button
    (real IdP); otherwise the dev email + role form posts to `action`."""
    err = f'<div class="card" style="border-color:#F5C4C6;background:#FDECEC;color:#B02A2F">{escape(error)}</div>' if error else ""
    if sso:
        form = (
            '<a class="btn sage" style="display:block;text-align:center;text-decoration:none" '
            f'href="{escape(action)}">Sign in with SSO</a>'
        )
    else:
        opts = "".join(
            f'<option value="{r.value}">{r.value.capitalize()}</option>' for r in _ASSIGNABLE_ROLES
        )
        form = (
            f'<form method="post" action="{escape(action)}">'
            '<label>Work email</label><input name="email" value="owner@northwind.com" autocomplete="username">'
            f'<label>Sign in as (demo)</label><select name="role">{opts}</select>'
            '<button class="btn" style="width:100%;margin-top:18px" type="submit">Sign in</button>'
            "</form>"
        )
    body = f"""<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:radial-gradient(1100px 460px at 50% -10%, #12352b, var(--rg-forest) 60%)">
      <div class="card" style="width:min(420px,92vw);padding:30px">
        <div style="text-align:center;margin-bottom:6px">
          <span style="font:800 22px/1 var(--rg-sans);letter-spacing:.18em;text-transform:uppercase;color:var(--rg-forest)">RGNR<span style="color:var(--rg-sage)">8</span></span>
        </div>
        <div style="text-align:center;color:var(--rg-muted);font-size:11px;letter-spacing:.16em;text-transform:uppercase;margin-bottom:20px">Bold. Timeless. Endless potential.</div>
        {err}{form}
      </div></div>"""
    return _doc("RGNR8 — sign in", body)


def _nav_html(tenant: str, permissions: frozenset[Permission], active: str) -> str:
    links = []
    for suffix, label, perm in _NAV:
        if perm not in permissions:
            continue
        href = f"/t/{escape(tenant)}" + (f"/{suffix}" if suffix else "")
        cls = "active" if active == (suffix or "cash") else ""
        links.append(f'<a class="{cls}" href="{href}">{escape(label)}</a>')
    return '<nav class="rg-nav">' + "".join(links) + "</nav>"


def render_shell(
    *,
    tenant: str,
    display_name: str,
    role: Role | None,
    permissions: frozenset[Permission],
    active: str,
    body_html: str,
    subject: str = "",
) -> str:
    """Wrap a screen body in the role-aware app shell (top bar + nav + identity)."""
    role_label = role.value.capitalize() if role is not None else "—"
    bar = (
        '<header class="rg-bar">'
        '<span class="rg-lockup">'
        f"{mark_svg(20, IVORY)}"
        '<span class="rg-wordmark" style="font-size:16px">RGNR<span class="rg-8">8</span></span></span>'
        f"{_nav_html(tenant, permissions, active)}"
        '<span class="rg-who">'
        f'<span>{escape(subject or display_name)}</span>'
        f'<span class="rg-rolechip">{escape(role_label)}</span>'
        '<a href="/logout">Sign out</a></span>'
        "</header>"
    )
    return _doc(f"RGNR8 — {display_name}", bar + f'<div class="shell">{body_html}</div>')


def render_app_home(
    *,
    tenant: str,
    display_name: str,
    role: Role | None,
    permissions: frozenset[Permission],
    cash_today: str,
    status: str,
    headline: str,
    latest_period: str | None,
) -> str:
    """The role-aware landing body. Owner (and everyone) leads with cash; the
    primary call-to-action shifts by role — owner/controller → review the
    outlook, bookkeeper/accountant → go to the close."""
    color = {"STABLE": "var(--rg-pos)", "WATCH": "var(--rg-watch)", "AT_RISK": "var(--rg-risk)"}.get(status, "var(--rg-muted)")
    # Distinct glyph per state so status never rides on color alone.
    glyph = {"STABLE": "✓", "WATCH": "◆", "AT_RISK": "▲"}.get(status, "●")
    status_label = status.replace("_", " ")
    # Owner/controller lead with the cash outlook; the accountant/bookkeeper live
    # in the bank feed, so their primary action goes straight to transactions.
    if role in (Role.BOOKKEEPER, Role.ACCOUNTANT) and Permission.VIEW_TRANSACTIONS in permissions:
        cta = f'<a class="btn sage" style="text-decoration:none" href="/t/{escape(tenant)}/transactions">Review bank transactions</a>'
    else:
        cta = f'<a class="btn sage" style="text-decoration:none" href="/t/{escape(tenant)}">Open the cash outlook</a>'
    pkg_link = (
        f'<a href="/t/{escape(tenant)}/packages/{escape(latest_period)}">View {escape(latest_period)} package</a>'
        if latest_period
        else '<span class="muted">No published package yet</span>'
    )
    hello = "Welcome back" if role != Role.ACCOUNTANT else "Welcome"
    return f"""<h1>{escape(hello)}</h1>
    <p class="sub">{escape(display_name)} · you're signed in as <strong>{role.value.capitalize() if role else '—'}</strong></p>
    <div class="card"><div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap">
      <div><div style="font-size:11px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;color:var(--rg-muted)">Cash today</div>
        <div style="font-size:40px;font-weight:800;letter-spacing:-.01em;font-variant-numeric:tabular-nums">{escape(cash_today)}</div></div>
      <div style="text-align:right;max-width:36ch">
        <span role="status" aria-label="Status: {escape(status_label)}" style="display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:4px 12px;font-weight:700;font-size:12px;text-transform:uppercase;color:{color};border:1px solid color-mix(in srgb, {color} 20%, transparent)"><span aria-hidden="true">{glyph}</span> {escape(status_label)}</span>
        <div style="margin-top:8px;color:var(--rg-ink-2);font-size:14px">{escape(headline)}</div></div>
    </div>
    <div class="row" style="margin-top:18px">{cta}
      <a class="btn ghost" style="text-decoration:none" href="/t/{escape(tenant)}/briefing">This week's briefing</a></div></div>
    <div class="card"><h2 style="margin-top:0;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--rg-muted)">Records</h2>
      <div class="row"><div class="grow">Latest sealed financial package</div>{pkg_link}</div></div>"""


def render_users_admin(tenant: str, members: Sequence[tuple[Membership, User | None]]) -> str:
    """The users/roles admin body (list + change-role + invite). Posts to the
    RBAC `/api/<tenant>/users` route via a small inline form."""
    def role_opts(cur: Role) -> str:
        return "".join(
            f'<option value="{r.value}"{" selected" if r == cur else ""}>{r.value.capitalize()}</option>'
            for r in _ASSIGNABLE_ROLES
        )

    rows = ""
    for m, u in members:
        disabled = " disabled" if m.role == Role.OWNER else ""
        email = escape(u.email if u is not None else m.user_id)
        name = escape(u.name if (u is not None and u.name) else "—")
        remove = "" if m.role == Role.OWNER else (
            "<button class='btn ghost' onclick=\"removeMember('" + email + "')\">Remove</button>"
        )
        rows += (
            "<tr><td><strong>" + name + "</strong></td><td class='muted'>" + email + "</td>"
            "<td><select data-user='" + email + "'" + disabled + " onchange='setRole(this)'>"
            + role_opts(m.role) + "</select></td>"
            "<td style='text-align:right'>" + remove + "</td></tr>"
        )
    invite_roles = "".join(
        f'<option value="{r.value}">{r.value.capitalize()}</option>'
        for r in _ASSIGNABLE_ROLES if r != Role.OWNER
    )
    body = f"""<h1>Team &amp; roles</h1>
    <p class="sub">Who can see and do what in {escape(tenant)}. Changes take effect immediately.</p>
    <div id="rgErr" class="banner warn" role="alert" style="display:none;margin-bottom:12px"></div>
    <table><thead><tr><th>Member</th><th>Email</th><th>Role</th><th></th></tr></thead><tbody>{rows}</tbody></table>
    <div class="card" style="margin-top:16px"><h2 style="margin-top:0;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--rg-muted)">Invite a teammate</h2>
      <div class="row"><input id="invEmail" class="grow" placeholder="name@company.com">
      <select id="invRole" style="width:auto">{invite_roles}</select>
      <button id="invBtn" class="btn" onclick="invite()">Send invite</button></div>
      <p class="muted" style="font-size:12px;margin-top:10px">Owner &amp; Controller can publish the close · Bookkeeper reconciles but can't seal · Accountant is your external CPA · Viewer is read-only.</p>
    </div>
    <script>
      const T={tenant!r};
      function rgErr(m){{ var b=document.getElementById('rgErr'); if(b){{ b.textContent=m||''; b.style.display=m?'block':'none'; }} }}
      function rgBusy(el,on,label){{ if(!el)return; el.disabled=on; if(el.tagName==='BUTTON'){{ if(on){{ el.dataset.rgPrev=el.dataset.rgPrev||el.textContent; el.textContent=label||'Working…'; }} else if(el.dataset.rgPrev!=null){{ el.textContent=el.dataset.rgPrev; }} }} }}
      function api(method, body){{ return fetch('/api/'+T+'/users', {{method, headers:{{'content-type':'application/json'}}, body: body?JSON.stringify(body):undefined, credentials:'same-origin'}}); }}
      async function run(ctl, label, body){{
        rgErr(''); rgBusy(ctl, true, label);
        try{{
          const r = await api('POST', body);
          if(!r.ok){{ rgErr('Could not save the change (HTTP '+r.status+'). Nothing was changed.'); rgBusy(ctl, false); return; }}
          location.reload();
        }} catch(e){{ rgErr('Network error — please try again.'); rgBusy(ctl, false); }}
      }}
      function setRole(sel){{ return run(sel, null, {{email: sel.dataset.user, role: sel.value}}); }}
      function removeMember(email){{ if(!confirm('Remove '+email+' from the team? They lose access to this business immediately.')) return;
        return run(null, null, {{email, role: null}}); }}
      function invite(){{ const e=document.getElementById('invEmail').value.trim();
        if(!e.includes('@')){{ rgErr('Enter a valid email address.'); return; }}
        return run(document.getElementById('invBtn'), 'Sending…', {{email:e, role: document.getElementById('invRole').value}}); }}
    </script>"""
    return body


def render_audit_log(tenant: str, events: Sequence[AuditEvent], *, configured: bool = True) -> str:
    """A client-facing 'who did what' view: the tenant's audit trail in a table,
    most-recent-first (the caller supplies the ordering). Reuses the shell + table
    CSS; wrap the returned body in `render_shell`."""
    if not configured:
        return (f"""<h1>Audit log</h1>
        <p class="sub">{escape(tenant)} · a record of who did what</p>
        <div class="banner warn">An audit log isn't configured for this workspace yet.</div>""")
    if not events:
        rows_html = '<div class="banner good">No activity has been recorded yet.</div>'
    else:
        rows = ""
        for e in events:
            when = datetime.fromtimestamp(e.at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            rows += (f'<tr><td class="muted">{escape(when)}</td>'
                     f'<td>{escape(e.actor or "—")}</td>'
                     f'<td><strong>{escape(e.action)}</strong></td>'
                     f'<td class="muted">{escape(e.target or "—")}</td>'
                     f'<td class="muted" style="font-size:12px">{escape(e.detail or "")}</td></tr>')
        rows_html = (
            '<div class="table-scroll"><table><thead><tr><th>When</th><th>Who</th>'
            '<th>Action</th><th>Target</th><th>Detail</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
        )
    return f"""<h1>Audit log</h1>
    <p class="sub">{escape(tenant)} · a record of who did what, most recent first</p>
    {rows_html}"""
