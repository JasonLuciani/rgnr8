"""Render a WeeklyBriefing to plain text (owner email) or self-contained HTML."""

from __future__ import annotations

from html import escape

from rgnr8_forecast.brand import POSITIVE, RISK, WATCH

from .models import StatusLevel, WeeklyBriefing

_BADGE = {
    StatusLevel.STABLE: "[ STABLE ]",
    StatusLevel.WATCH: "[ WATCH ]",
    StatusLevel.AT_RISK: "[ AT RISK ]",
}
_HTML_COLOR = {
    StatusLevel.STABLE: POSITIVE,
    StatusLevel.WATCH: WATCH,
    StatusLevel.AT_RISK: RISK,
}


def render_text(b: WeeklyBriefing) -> str:
    ccy = b.currency
    lines: list[str] = []
    lines.append(f"{_BADGE[b.status]}  RGNR8 weekly cash briefing — {b.period_label}")
    lines.append("=" * 72)
    lines.append(b.headline)
    lines.append(f"({b.status_reason})")
    if b.primary_action:
        lines.append("")
        lines.append("DO THIS:")
        lines.append("  " + b.primary_action)
    lines.append("")
    lines.append("THE NUMBERS")
    for f in b.facts:
        val = f"{ccy} {f.amount.to_decimal_string()}" if f.amount is not None else (f.text or "")
        extra = f"  ({f.text})" if f.amount is not None and f.text else ""
        lines.append(f"  {f.label:<28} {val}{extra}")
    if b.drivers:
        lines.append("")
        lines.append("WHAT'S USING YOUR CASH")
        for d in b.drivers:
            lines.append(
                f"  {d.label:<24} {ccy} {d.amount.to_decimal_string():>12}  "
                f"({d.share_bps / 100:.0f}% of outflows)"
            )
    lines.append("")
    lines.append("13-WEEK GLANCE (closing balance)")
    for w in b.week_glance:
        mark = {StatusLevel.STABLE: " ", StatusLevel.WATCH: ".", StatusLevel.AT_RISK: "!"}[w.status]
        lines.append(f"  wk{w.index:<2} {w.start.isoformat()}  {ccy} {w.closing.to_decimal_string():>12}  {mark}")
    if b.data_quality:
        lines.append("")
        lines.append("HEADS-UP (data quality)")
        for n in b.data_quality:
            lines.append(f"  - {n}")
    lines.append("")
    lines.append(f"confidence {b.overall_confidence}/100 · version {b.version_id}")
    return "\n".join(lines)


def render_html(b: WeeklyBriefing) -> str:
    ccy = b.currency
    color = _HTML_COLOR[b.status]

    def money_rows(items: list[tuple[str, str, str]]) -> str:
        return "".join(
            f'<tr><td class="lbl">{escape(l)}</td><td class="val">{escape(v)}</td>'
            f'<td class="sub">{escape(s)}</td></tr>'
            for l, v, s in items
        )

    facts = [
        (f.label, f"{ccy} {f.amount.to_decimal_string()}" if f.amount is not None else (f.text or ""),
         f.text if (f.amount is not None and f.text) else "")
        for f in b.facts
    ]
    drivers = [
        (d.label, f"{ccy} {d.amount.to_decimal_string()}", f"{d.share_bps / 100:.0f}% of outflows")
        for d in b.drivers
    ]
    glance = "".join(
        f'<tr><td>wk{w.index}</td><td>{w.start.isoformat()}</td>'
        f'<td class="val">{ccy} {w.closing.to_decimal_string()}</td>'
        f'<td><span class="dot" style="background:{_HTML_COLOR[w.status]}"></span></td></tr>'
        for w in b.week_glance
    )
    action = (
        f'<div class="action"><strong>Do this:</strong> {escape(b.primary_action)}</div>'
        if b.primary_action else ""
    )
    dq = (
        "<h3>Heads-up</h3><ul>" + "".join(f"<li>{escape(n)}</li>" for n in b.data_quality) + "</ul>"
        if b.data_quality else ""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 weekly cash briefing</title>
<style>
  body {{ font: 400 15px/1.55 system-ui,-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: #0E241E; margin: 0; background: #F2EFE6; -webkit-font-smoothing:antialiased; }}
  .card {{ max-width: 680px; margin: 24px auto; background: #fff; border-radius: 16px;
          box-shadow: 0 1px 2px rgba(14,36,30,.07),0 1px 3px rgba(14,36,30,.05); overflow: hidden;
          border:1px solid #E2DFD3; }}
  .band {{ background: {color}; color: #F2EFE6; padding: 18px 24px; }}
  .wm {{ font: 600 14px/1 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; letter-spacing:.22em;
         text-transform:uppercase; opacity:.95; margin-bottom:12px; }}
  .band .badge {{ font-weight: 700; letter-spacing: .07em; text-transform: uppercase; font-size: 12px; opacity: .95; }}
  .band h1 {{ font-family:ui-serif,Georgia,"Times New Roman",serif; font-size: 22px; margin: 6px 0 2px; font-weight:700; letter-spacing:-.01em; }}
  .band p {{ margin: 0; opacity: .94; font-size: 13px; }}
  .body {{ padding: 20px 24px; }}
  .action {{ background: #EAF0EA; border: 1px solid #CBD9CC; border-radius: 10px; padding: 12px 14px; margin: 0 0 18px; }}
  h3 {{ font-size: 11px; text-transform: uppercase; letter-spacing: .09em; color: #6E7A70; margin: 20px 0 8px; font-weight:700; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 7px 0; border-bottom: 1px solid #EFEDE3; font-size: 14px; }}
  .lbl {{ color: #2C3B33; }} .val {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 700; }}
  .sub {{ color: #9AA196; font-size: 12px; padding-left: 10px; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; }}
  .foot {{ color: #9AA196; font-size: 12px; padding: 14px 24px; border-top: 1px solid #EFEDE3; }}
</style></head>
<body><div class="card">
  <div class="band">
    <div class="wm">RGNR8</div>
    <div class="badge">{escape(b.status.value.replace("_", " "))}</div>
    <h1>{escape(b.headline)}</h1>
    <p>{escape(b.status_reason)}</p>
  </div>
  <div class="body">
    {action}
    <h3>The numbers</h3><table>{money_rows(facts)}</table>
    {"<h3>What's using your cash</h3><table>" + money_rows(drivers) + "</table>" if drivers else ""}
    <h3>13-week glance</h3><table>{glance}</table>
    {dq}
  </div>
  <div class="foot">Confidence {b.overall_confidence}/100 · {escape(b.period_label)} · version {escape(b.version_id)}</div>
</div></body></html>"""
