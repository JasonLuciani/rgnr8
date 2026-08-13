"""In-shell screen bodies: Cash, Briefing, Close, and Package.

These are the bodies the app shell (`render_shell`) wraps, so every owner-facing
surface lives inside the same forest top-bar + role-aware nav — not a standalone
full page. Each one renders from the same engines the standalone views use
(`run_forecast` → `build_briefing` → the ask-your-CFO `answer`er, the published
financial-package reader, and the close board below), so numbers are identical to
the deep-linked pages; only the chrome differs.

Everything is self-contained (system fonts, inline SVG, no external assets) — the
same audit the other renderers pass.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from rgnr8_forecast import ForecastResult, Money
from rgnr8_forecast.brand import format_money as _money  # the one shared money formatter
from rgnr8_briefing import Question, WeeklyBriefing, answer, build_briefing
from rgnr8_briefing.answer import SUGGESTED
from rgnr8_briefing.today import _cash_chart, _chart_points  # shared 13-week chart
from rgnr8_ar import ARReport, ChaseItem, CollectionNudge
from rgnr8_ar.aging import BUCKET_ORDER, AgingBucket
from rgnr8_reports import ReportSpec

_STATUS = {
    "STABLE": ("Stable", "var(--rg-pos)"),
    "WATCH": ("Watch", "var(--rg-watch)"),
    "AT_RISK": ("At risk", "var(--rg-risk)"),
}
# Distinct glyph per state so status never rides on color alone.
_STATUS_GLYPH = {"STABLE": "✓", "WATCH": "◆", "AT_RISK": "▲"}


def _kpi_value(m: Money) -> str:
    """A KPI money value, colored with the risk token when it's negative (a
    floor breach / no cushion) so the alarming numbers actually stand out."""
    v = escape(_money(m))
    return f'<span class="neg">{v}</span>' if m.minor_units < 0 else v


def _status_pill(status: str) -> str:
    label, color = _STATUS.get(status, (status, "var(--rg-muted)"))
    glyph = _STATUS_GLYPH.get(status, "●")
    return (f'<span class="pill" role="status" aria-label="Status: {escape(label)}" '
            f'style="color:{color};border:1px solid color-mix(in srgb, {color} 20%, transparent)">'
            f'<span aria-hidden="true">{glyph}</span> {escape(label)}</span>')


# --- Cash (in-shell owner dashboard) ----------------------------------------
def _forecast_is_empty(forecast: ForecastResult) -> bool:
    """True for a freshly-onboarded owner with no usable forecast yet — no
    projected weeks, or a zero opening balance with nothing flowing."""
    p = forecast.projection
    return not p.weeks or (p.opening_available.minor_units == 0 and not forecast.flows)


def _cash_empty_state(display_name: str) -> str:
    """The designed first-run / degraded state: nothing to chart yet, so invite
    the owner to connect a bank rather than show empty tiles and a flat line."""
    return f"""<h1>Cash outlook</h1>
    <p class="sub">{escape(display_name)}</p>
    <div class="card empty">
      <div style="font-family:var(--rg-serif);font-size:22px;margin-bottom:8px">Connect your bank to see your cash</div>
      <p class="muted" style="max-width:48ch;margin:0 auto 18px">RGNR8 builds your 13-week cash outlook the moment your first bank feed lands.
      Connect an account and your minimum-cash floor, projected low point, and cushion appear right here.</p>
      <a class="btn sage" style="text-decoration:none" href="#connect">Connect your bank</a>
    </div>"""


def render_cash_body(forecast: ForecastResult, display_name: str) -> str:
    """The owner's cash outlook, drawn inside the shell: status hero, KPI tiles,
    the 13-week chart, and the cash drivers. Same numbers as the deep-link page."""
    if _forecast_is_empty(forecast):
        return _cash_empty_state(display_name)
    b = build_briefing(forecast)
    p = forecast.projection
    # "Cash today" leads the hero below, so the tile row leads with floor /
    # low point / cushion (no redundant Cash-today tile).
    tiles = [
        ("Minimum-cash floor", p.effective_floor),
        ("Projected low point", p.trough.balance),
        ("Cushion at low point", p.trough.balance - p.effective_floor),
    ]
    tile_html = "".join(f'<div class="tile"><div class="k">{escape(k)}</div>'
                        f'<div class="v">{_kpi_value(v)}</div></div>' for k, v in tiles)
    drivers = ""
    if b.drivers:
        top = b.drivers[0].amount.minor_units or 1
        for d in b.drivers:
            pct = round(d.amount.minor_units * 100 / top)
            drivers += (f'<div class="drv"><div class="t"><span>{escape(d.label)}</span>'
                        f'<span class="num">{escape(_money(d.amount))}</span></div>'
                        f'<div class="track"><div class="fill" style="width:{pct}%"></div></div>'
                        f'<div class="s">{d.share_bps/100:.0f}% of outflows</div></div>')
        drivers = f'<h2>What\'s using your cash</h2><div class="card">{drivers}</div>'
    action = ""
    if b.primary_action:
        action = (f'<div class="banner warn" style="background:var(--rg-ivory);color:var(--rg-ink);'
                  f'border-color:var(--rg-line)"><strong>Do this — </strong>{escape(b.primary_action)}</div>')
    _ = _chart_points  # (chart script omitted in-shell; SVG is static here)
    return f"""<h1>Cash outlook</h1>
    <p class="sub">{escape(display_name)} · as of {escape(p.as_of.isoformat())} · {escape(b.period_label)}</p>
    <div class="card"><div class="row" style="justify-content:space-between;align-items:flex-start">
      <div><div class="tile" style="border:none;padding:0"><div class="k">Cash today</div>
        <div class="v" style="font-size:40px">{escape(_money(p.opening_available))}</div></div></div>
      <div style="text-align:right;max-width:38ch">{_status_pill(b.status.value)}
        <div style="margin-top:8px;color:var(--rg-ink-2);font-size:14px">{escape(b.headline)}</div></div>
    </div>{action}</div>
    <div class="tiles">{tile_html}</div>
    <h2>Next 13 weeks — projected cash</h2>
    <div class="card">{_cash_chart(forecast)}</div>
    {drivers}
    <p class="muted" style="font-size:12px">Confidence {b.overall_confidence}/100 · every figure traces to the forecast · version {escape(b.version_id)}</p>"""


# --- Briefing (in-shell weekly briefing) ------------------------------------
def render_briefing_body(forecast: ForecastResult, display_name: str) -> str:
    """This week's owner briefing inside the shell: the status call, the primary
    action, the facts + drivers, the 13-week glance, and ask-your-CFO."""
    b = build_briefing(forecast)
    answers = {q.value: answer(forecast, q) for q in Question}
    facts = ""
    for f in b.facts:
        val = _money(f.amount) if f.amount is not None else (f.text or "")
        facts += (f'<tr><td>{escape(f.label)}</td><td class="num">{escape(val)}</td>'
                  f'<td class="muted" style="font-size:12px">{escape(f.evidence.note or f.evidence.field or "forecast")}</td></tr>')
    drivers = ""
    if b.drivers:
        top = b.drivers[0].amount.minor_units or 1
        for d in b.drivers:
            pct = round(d.amount.minor_units * 100 / top)
            drivers += (f'<div class="drv"><div class="t"><span>{escape(d.label)}</span>'
                        f'<span class="num">{escape(_money(d.amount))}</span></div>'
                        f'<div class="track"><div class="fill" style="width:{pct}%"></div></div></div>')
    glance = ""
    for w in b.week_glance:
        _, gc = _STATUS.get(w.status.value, ("", "var(--rg-muted)"))
        glance += (f'<tr><td>Week {w.index}</td><td class="muted">{escape(w.start.isoformat())}</td>'
                   f'<td class="num">{escape(_money(w.closing))}</td>'
                   f'<td><span class="dot" style="background:{gc}"></span>{escape(w.status.value)}</td></tr>')
    dq = ""
    if b.data_quality:
        items = "".join(f"<li>{escape(n)}</li>" for n in b.data_quality)
        dq = f'<h2>Heads-up</h2><div class="card"><ul style="margin:0;padding-left:18px;color:var(--rg-ink-2)">{items}</ul></div>'
    action = (f'<div class="banner warn" style="background:var(--rg-ivory);color:var(--rg-ink);border-color:var(--rg-line)">'
              f'<strong>Do this — </strong>{escape(b.primary_action)}</div>') if b.primary_action else ""
    chips = "".join(f'<button class="chip" onclick="ans({q.value!r})">{escape(text)}</button>'
                    for q, text in SUGGESTED)
    answers_json = json.dumps({k: {"t": a.answer_text, "q": a.question_text} for k, a in answers.items()})
    return f"""<h1>This week's briefing</h1>
    <p class="sub">{escape(display_name)} · {escape(b.period_label)} · as of {escape(b.as_of.isoformat())}</p>
    <div class="card"><div class="row" style="justify-content:space-between">
      {_status_pill(b.status.value)}
      <span class="muted" style="font-size:12px">Confidence {b.overall_confidence}/100</span></div>
      <div style="font-family:var(--rg-serif);font-size:22px;margin:10px 0 4px">{escape(b.headline)}</div>
      <p class="muted" style="margin:0">{escape(b.status_reason)}</p>{action}</div>
    <h2>The numbers</h2>
    <table><thead><tr><th>Fact</th><th class="num">Amount</th><th>Backed by</th></tr></thead><tbody>{facts}</tbody></table>
    <h2>What's moving cash</h2><div class="card">{drivers or '<span class="muted">No drivers this period.</span>'}</div>
    <h2>13-week glance</h2>
    <table><thead><tr><th>Week</th><th>Starting</th><th class="num">Closing</th><th>Status</th></tr></thead><tbody>{glance}</tbody></table>
    {dq}
    <h2>Ask your CFO</h2>
    <div class="card"><div class="row">{chips}</div>
      <div id="answer" class="banner" style="display:none;margin-top:14px;background:var(--rg-ivory);color:var(--rg-ink);border-color:var(--rg-line)">
        <div id="aq" class="muted" style="font-size:12px"></div><div id="at" style="font-weight:400"></div></div></div>
    <script>
      const A={answers_json};
      function ans(k){{const a=A[k];const box=document.getElementById('answer');
        document.getElementById('aq').textContent=a.q;document.getElementById('at').textContent=a.t;
        box.style.display='block';}}
    </script>"""


# --- Close board (view model + in-shell screen) -----------------------------
_CLOSE_STATUS = {
    "done": ("Done", "var(--rg-pos)"),
    "open": ("Open", "var(--rg-muted)"),
    "overdue": ("Overdue", "var(--rg-risk)"),
    "blocked": ("Blocked", "var(--rg-watch)"),
}
_CLOSE_NEXT = {"open": "done", "overdue": "done", "done": "open", "blocked": "open"}


@dataclass(frozen=True, slots=True)
class CloseTask:
    """One line on the month-end close checklist (mirrors a `@rgnr8/close`
    calendar task): what it is, who owns it, when it's due, and where it stands."""

    key: str
    label: str
    status: str = "open"  # done | open | overdue | blocked
    owner: str = ""       # role responsible (e.g. "Bookkeeper")
    due: str = ""         # ISO date
    note: str = ""


@dataclass(frozen=True, slots=True)
class CloseBoard:
    period: str  # "2026-08"
    tasks: tuple[CloseTask, ...] = ()
    sealed: bool = False  # a financial package has been published for the period

    @property
    def total(self) -> int:
        return len(self.tasks)

    @property
    def done(self) -> int:
        return sum(1 for t in self.tasks if t.status == "done")

    @property
    def overdue(self) -> int:
        return sum(1 for t in self.tasks if t.status == "overdue")

    @property
    def blocked(self) -> int:
        return sum(1 for t in self.tasks if t.status == "blocked")

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.done == self.total

    def with_task_status(self, key: str, status: str) -> "CloseBoard":
        from dataclasses import replace
        tasks = tuple(replace(t, status=status) if t.key == key else t for t in self.tasks)
        return replace(self, tasks=tasks)


def render_close_body(board: CloseBoard, tenant: str, *, can_manage: bool, can_publish: bool) -> str:
    """The month-end close checklist inside the shell. `can_manage` (MANAGE_CLOSE)
    toggles the advance controls; `can_publish` (PUBLISH_CLOSE) gates the seal.
    Sealing is only offered once every task is done — the publish gate."""
    pct = round(board.done * 100 / board.total) if board.total else 0
    rows = ""
    for t in board.tasks:
        label, color = _CLOSE_STATUS.get(t.status, (t.status, "var(--rg-muted)"))
        advance = ""
        if can_manage and not board.sealed and t.status != "done":
            advance = ('<button class="btn sage" style="padding:5px 10px" '
                       f"onclick=\"advance(this,'{escape(t.key)}')\">Mark done</button>")
        elif can_manage and not board.sealed and t.status == "done":
            advance = ('<button class="btn ghost" style="padding:5px 10px" '
                       f"onclick=\"reopen(this,'{escape(t.key)}')\">Reopen</button>")
        rows += (f'<tr><td><strong>{escape(t.label)}</strong>'
                 + (f'<br><span class="muted" style="font-size:12px">{escape(t.note)}</span>' if t.note else "")
                 + f'</td><td class="muted">{escape(t.owner or "—")}</td>'
                 f'<td class="muted">{escape(t.due or "—")}</td>'
                 f'<td><span class="dot" style="background:{color}"></span>{escape(label)}</td>'
                 f'<td style="text-align:right">{advance}</td></tr>')
    if board.sealed:
        banner_cls, banner = "good", f"{escape(board.period)} is closed and the financial package is sealed."
    elif board.complete:
        banner_cls, banner = "good", f"All {board.total} tasks done — {escape(board.period)} is ready to seal."
    else:
        tail = f" · {board.overdue} overdue" if board.overdue else ""
        banner_cls, banner = "warn", f"{board.done} of {board.total} done{tail} — {escape(board.period)} close in progress."
    seal = ""
    if not board.sealed:
        if board.complete and can_publish:
            seal = ('<button id="sealBtn" class="btn" onclick="publish(this)">Seal &amp; publish package</button>')
        elif board.complete and not can_publish:
            seal = '<span class="muted">Ready to seal — an owner, controller, or accountant must publish.</span>'
        else:
            seal = '<span class="muted">Finish every task to seal the period.</span>'
    err_block = ('<div id="rgErr" class="banner warn" role="alert" style="display:none;margin:12px 0"></div>'
                 if (can_manage or can_publish) else "")
    js = f"""<script>
      const T={tenant!r};
      function rgErr(m){{ var b=document.getElementById('rgErr'); if(b){{ b.textContent=m||''; b.style.display=m?'block':'none'; }} }}
      function rgBusy(el,on,label){{ if(!el)return; el.disabled=on; if(el.tagName==='BUTTON'){{ if(on){{ el.dataset.rgPrev=el.dataset.rgPrev||el.textContent; el.textContent=label||'Working…'; }} else if(el.dataset.rgPrev!=null){{ el.textContent=el.dataset.rgPrev; }} }} }}
      function post(u,b){{return fetch(u,{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify(b||{{}}),credentials:'same-origin'}});}}
      async function run(ctl,label,u,b){{
        rgErr(''); rgBusy(ctl, true, label);
        try{{
          const r = await post(u,b);
          if(!r.ok){{ rgErr('Could not complete that action (HTTP '+r.status+').'); rgBusy(ctl, false); return; }}
          location.reload();
        }} catch(e){{ rgErr('Network error — please try again.'); rgBusy(ctl, false); }}
      }}
      function advance(ctl,id){{ return run(ctl, 'Saving…', '/api/'+T+'/close', {{id,status:'done'}}); }}
      function reopen(ctl,id){{ return run(ctl, 'Saving…', '/api/'+T+'/close', {{id,status:'open'}}); }}
      function publish(ctl){{ if(!confirm('Seal '+T+' and publish the financial package? This is permanent and cannot be undone.')) return;
        return run(ctl, 'Sealing…', '/api/'+T+'/close/publish', {{}}); }}
    </script>""" if (can_manage or can_publish) else ""
    return f"""<h1>Month-end close</h1>
    <p class="sub">{escape(board.period)} · the checklist that has to clear before the books are sealed</p>
    <div class="banner {banner_cls}">{banner}</div>
    {err_block}
    <div class="card"><div class="row" style="justify-content:space-between"><span class="muted" style="font-size:12px">Progress</span>
      <span class="muted" style="font-size:12px">{board.done}/{board.total}</span></div>
      <div class="bar" style="margin-top:6px"><span style="width:{pct}%"></span></div></div>
    <table><thead><tr><th>Task</th><th>Owner</th><th>Due</th><th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table>
    <div class="card" style="margin-top:16px"><div class="row" style="justify-content:space-between">
      <div><strong>Seal the period</strong><div class="muted" style="font-size:12px">Publishing writes the immutable financial package.</div></div>
      {seal}</div></div>
    {js}"""


def default_close_board(period: str) -> CloseBoard:
    """A representative close checklist for demos/onboarding before the real
    `@rgnr8/close` calendar is wired for a tenant."""
    return CloseBoard(period=period, tasks=(
        CloseTask("feeds", "Bank & card feeds imported", "done", "Bookkeeper", f"{period}-01"),
        CloseTask("categorize", "All transactions categorized", "open", "Bookkeeper", f"{period}-03"),
        CloseTask("reconcile", "Bank reconciliations tie out", "open", "Bookkeeper", f"{period}-04"),
        CloseTask("payroll", "Payroll & benefits accrued", "open", "Controller", f"{period}-04"),
        CloseTask("review", "Owner/controller review", "open", "Controller", f"{period}-05"),
    ))


# --- Published financial packages (in-shell records screen) -----------------
def render_packages_body(tenant: str, periods: Sequence[str], configured: bool) -> str:
    """The sealed financial-package records inside the shell — each period links
    to its verified package view."""
    if not configured:
        return ("""<h1>Financial packages</h1>
        <p class="sub">Sealed month-end records</p>
        <div class="banner warn">Financial packages aren't configured for this workspace yet. """
                """They appear here the first time a month-end close is sealed.</div>""")
    if not periods:
        body = '<div class="banner warn">No package has been sealed yet. Finish a close to publish the first one.</div>'
    else:
        rows = "".join(
            f'<tr><td><strong>{escape(pr)}</strong></td><td class="muted">Sealed · integrity-verified</td>'
            f'<td style="text-align:right"><a class="btn ghost" style="text-decoration:none" '
            f'href="/t/{escape(tenant)}/packages/{escape(pr)}">Open package</a></td></tr>'
            for pr in sorted(periods, reverse=True)
        )
        body = (f'<table><thead><tr><th>Period</th><th>Status</th><th></th></tr></thead>'
                f'<tbody>{rows}</tbody></table>')
    return f"""<h1>Financial packages</h1>
    <p class="sub">{escape(tenant)} · sealed month-end records · each figure re-verified against its fingerprint</p>
    {body}"""


# --- Reports (in-shell baseline + saved report index) -----------------------
def render_reports_list(
    tenant: str,
    baselines: Sequence[ReportSpec],
    saved: Sequence[ReportSpec],
) -> str:
    """The reports index inside the shell: the baseline library and this tenant's
    saved custom reports. Each row links to the in-shell render plus JSON and CSV
    exports. Self-contained (relative links only), reuses the shell table/card CSS.
    """
    def _rows(specs: Sequence[ReportSpec]) -> str:
        out = ""
        for spec in specs:
            open_href = f"/t/{escape(tenant)}/reports/{escape(spec.id)}"
            json_href = f"/api/{escape(tenant)}/reports/{escape(spec.id)}.json"
            csv_href = f"/api/{escape(tenant)}/reports/{escape(spec.id)}.csv"
            desc = (f'<br><span class="muted" style="font-size:12px">{escape(spec.description)}</span>'
                    if spec.description else "")
            out += (
                f'<tr><td><strong>{escape(spec.title)}</strong>{desc}</td>'
                f'<td style="text-align:right">'
                f'<a class="btn ghost" style="text-decoration:none" href="{open_href}">Open</a> '
                f'<a class="muted" href="{json_href}">JSON</a> · '
                f'<a class="muted" href="{csv_href}">CSV</a></td></tr>'
            )
        return out

    baseline_table = (
        '<table><thead><tr><th>Report</th><th></th></tr></thead>'
        f'<tbody>{_rows(baselines)}</tbody></table>'
    )
    if saved:
        saved_block = (
            '<h2>Saved reports</h2>'
            '<table><thead><tr><th>Report</th><th></th></tr></thead>'
            f'<tbody>{_rows(saved)}</tbody></table>'
        )
    else:
        saved_block = (
            '<h2>Saved reports</h2>'
            '<div class="banner good">No custom reports saved yet — '
            'build one from a section list and it appears here.</div>'
        )
    return f"""<h1>Reports</h1>
    <p class="sub">{escape(tenant)} · ready-made baseline reports and your saved custom reports · open in-app or export to JSON / CSV</p>
    <h2>Baseline reports</h2>
    {baseline_table}
    {saved_block}"""


# --- Scenario planning (in-shell what-if owner screen) ----------------------
# The owner-facing templates, mapped to the `@rgnr8/scenario` library builders.
# Each entry: (template key, label, blurb, [(param, label, input-type, placeholder)]).
_SCENARIO_TEMPLATES: tuple[tuple[str, str, str, tuple[tuple[str, str, str, str], ...]], ...] = (
    ("hire", "Hire someone", "A new recurring monthly payroll cost.",
     (("monthly_cost", "Monthly cost", "text", "8000.00"),
      ("start", "Start date", "date", ""))),
    ("customer_pays_late", "Customer pays late", "A key customer slips their payment timing.",
     (("customer_id", "Customer", "text", "acme"),
      ("days", "Days later than usual", "number", "30"))),
    ("take_loan", "Take a loan", "A lump inflow now, repaid monthly.",
     (("amount", "Loan amount", "text", "50000.00"),
      ("on", "Draw date", "date", ""),
      ("monthly_repayment", "Monthly repayment", "text", "2200.00"),
      ("first_repayment", "First repayment", "date", ""))),
    ("one_time_expense", "One-time expense", "A single dated cash outflow.",
     (("label", "What is it", "text", "New equipment"),
      ("amount", "Amount", "text", "12000.00"),
      ("on", "Date", "date", ""))),
)


def render_scenario_body(tenant: str, display_name: str) -> str:
    """The what-if planning screen inside the shell: pick a library template, fill
    its inputs, and see the resulting `ScenarioDiff` — trough Δ, breach week
    before/after, cushion Δ — rendered against the tenant's live forecast. The form
    POSTs the spec to `/api/<tenant>/scenario` and renders the returned diff;
    everything is self-contained (no external assets)."""
    cards = ""
    for key, label, blurb, params in _SCENARIO_TEMPLATES:
        fields = ""
        for pkey, plabel, ptype, placeholder in params:
            ph = f' placeholder="{escape(placeholder)}"' if placeholder else ""
            fields += (f'<label>{escape(plabel)}</label>'
                       f'<input data-param="{escape(pkey)}" type="{escape(ptype)}"{ph}>')
        cards += (
            f'<form class="card scn" data-template="{escape(key)}" onsubmit="return runScenario(this)">'
            f'<div style="font-family:var(--rg-serif);font-size:18px">{escape(label)}</div>'
            f'<p class="muted" style="margin:2px 0 4px;font-size:13px">{escape(blurb)}</p>'
            f'{fields}'
            f'<button class="btn sage" style="margin-top:14px" type="submit">Run scenario</button>'
            f'</form>'
        )
    return f"""<h1>Scenario planning</h1>
    <p class="sub">{escape(display_name)} · model a decision against your live 13-week cash outlook — nothing is saved</p>
    <div id="rgErr" class="banner warn" role="alert" style="display:none">Something went wrong.</div>
    <div id="scnResult" style="display:none"></div>
    <div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">{cards}</div>
    <script>
      const T={tenant!r};
      function rgErr(m){{ var b=document.getElementById('rgErr'); if(b){{ b.textContent=m||''; b.style.display=m?'block':'none'; }} }}
      function fmtWeek(w){{ return (w===null||w===undefined) ? 'no breach' : ('week '+w); }}
      function renderDiff(d){{
        var box=document.getElementById('scnResult');
        box.innerHTML =
          '<h2>'+d.scenario+'</h2>'+
          '<div class="tiles">'+
          '<div class="tile"><div class="k">Low-point change</div><div class="v">'+d.trough_delta+'</div></div>'+
          '<div class="tile"><div class="k">Cushion change</div><div class="v">'+d.cushion_delta+'</div></div>'+
          '<div class="tile"><div class="k">Breach before</div><div class="v">'+fmtWeek(d.breach_week_before)+'</div></div>'+
          '<div class="tile"><div class="k">Breach after</div><div class="v">'+fmtWeek(d.breach_week_after)+'</div></div>'+
          '</div>';
        box.style.display='block';
        box.scrollIntoView({{behavior:'smooth',block:'start'}});
      }}
      async function runScenario(form){{
        rgErr('');
        var params={{}};
        form.querySelectorAll('[data-param]').forEach(function(el){{ if(el.value!=='') params[el.dataset.param]=el.value; }});
        try{{
          var r = await fetch('/api/'+T+'/scenario', {{method:'POST', headers:{{'content-type':'application/json'}},
            body: JSON.stringify({{template: form.dataset.template, params: params}}), credentials:'same-origin'}});
          if(!r.ok){{ rgErr('Could not run that scenario (HTTP '+r.status+'). Check the inputs.'); return false; }}
          renderDiff(await r.json());
        }} catch(e){{ rgErr('Network error — please try again.'); }}
        return false;
      }}
    </script>"""


# --- AR / collections (in-shell owner screen) -------------------------------
_BUCKET_LABEL: dict[AgingBucket, str] = {
    AgingBucket.CURRENT: "Current",
    AgingBucket.D1_30: "1–30 days",
    AgingBucket.D31_60: "31–60 days",
    AgingBucket.D60_PLUS: "60+ days",
}
# The nudge tone, colored by escalation so a final notice reads as urgent.
_TONE_COLOR: dict[str, str] = {
    "FRIENDLY": "var(--rg-pos)", "FIRM": "var(--rg-watch)", "FINAL": "var(--rg-risk)"}


def render_ar_body(
    tenant: str,
    display_name: str,
    report: ARReport,
    chase: Sequence[ChaseItem],
    nudges: Sequence[CollectionNudge],
) -> str:
    """The receivables / collections screen inside the shell: the aging summary,
    the prioritized chase list (worst-first), and the per-invoice nudge drafts
    whose tone escalates with age. All computed by `@rgnr8/ar` over the tenant's
    open invoices. Read-only and self-contained."""
    aging = report.aging
    # Aging summary tiles (per bucket) + a total.
    tiles = ""
    for bucket in BUCKET_ORDER:
        tiles += (f'<div class="tile"><div class="k">{escape(_BUCKET_LABEL[bucket])}</div>'
                  f'<div class="v">{escape(_money(aging.amount(bucket)))}</div></div>')
    tiles += (f'<div class="tile"><div class="k">Total AR</div>'
              f'<div class="v">{escape(_money(report.total_ar))}</div></div>')
    dso = "" if report.dso is None else f" · DSO ~{report.dso:.0f} days"

    if not chase:
        chase_block = ('<div class="banner good">Nothing overdue — every open invoice is current '
                       f'as of {escape(report.as_of.isoformat())}.</div>')
    else:
        rows = ""
        for it in chase:
            inv = it.invoice
            rows += (f'<tr><td><strong>{escape(inv.id)}</strong></td>'
                     f'<td class="muted">{escape(inv.customer_id)}</td>'
                     f'<td class="num">{escape(_money(inv.open_amount))}</td>'
                     f'<td class="num">{it.days_overdue}</td>'
                     f'<td>{escape(_BUCKET_LABEL[it.bucket])}</td></tr>')
        chase_block = (f'<h2>Chase list — worst first</h2>'
                       f'<div class="table-scroll"><table><thead><tr><th>Invoice</th><th>Customer</th>'
                       f'<th class="num">Open</th><th class="num">Days overdue</th><th>Bucket</th>'
                       f'</tr></thead><tbody>{rows}</tbody></table></div>')

    drafts = ""
    for n in nudges:
        color = _TONE_COLOR.get(n.tone.value, "var(--rg-muted)")
        drafts += (
            f'<div class="card" style="margin-bottom:12px">'
            f'<div class="row" style="justify-content:space-between">'
            f'<strong>{escape(n.invoice.id)} · {escape(n.invoice.customer_id)}</strong>'
            f'<span class="pill" style="color:{color};border:1px solid color-mix(in srgb, {color} 20%, transparent)">'
            f'{escape(n.tone.value)}</span></div>'
            f'<div style="margin-top:8px;font-weight:700;font-size:13px">{escape(n.subject)}</div>'
            f'<pre style="white-space:pre-wrap;font:inherit;color:var(--rg-ink-2);margin:8px 0 0">{escape(n.body)}</pre>'
            f'</div>'
        )
    drafts_block = (f'<h2>Reminder drafts</h2>{drafts}' if drafts else "")
    return f"""<h1>Receivables &amp; collections</h1>
    <p class="sub">{escape(display_name)} · open AR as of {escape(report.as_of.isoformat())}{dso} · get paid faster</p>
    <div class="banner {'warn' if report.overdue_total.is_positive else 'good'}">
      {escape(_money(report.overdue_total))} overdue of {escape(_money(report.total_ar))} total AR.</div>
    <h2>Aging</h2>
    <div class="tiles">{tiles}</div>
    {chase_block}
    {drafts_block}"""
