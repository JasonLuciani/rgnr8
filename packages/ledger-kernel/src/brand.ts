/**
 * RGNR8 brand system — the single source of truth for how the tool looks (TS).
 *
 * Built to RGNR8's brand guide: **Forest Green, Ivory, Sage** — bold, timeless,
 * endless potential. Mirror of the Python `rgnr8_forecast.brand` module with
 * byte-identical tokens, so the TypeScript-rendered surfaces (QBO diff,
 * connector health, close calendar) are visually the same product as the Python
 * ones (Today, package, fleet). Self-contained: system fonts only, no external
 * assets or URLs.
 *
 * Palette: Forest Green #0E241E (primary/ink/bar), Ivory #F2EFE6 (the light),
 * Sage Green #5E7F63 (the one accent — mark, links, data series, focus). Status
 * stays muted/earthy: positive #3E7C5A, watch #B7791F, risk #B4443C.
 */

// Brand palette (keep in lockstep with brand.py) ------------------------------
export const RG = {
  forest: "#0E241E",
  ivory: "#F2EFE6",
  sage: "#5E7F63",
  ink: "#0E241E",
  ink2: "#2C3B33",
  muted: "#616D66",
  paper: "#FFFFFF",
  surface: "#F2EFE6",
  line: "#E2DFD3",
  accent: "#5E7F63",
  accentDeep: "#42604A",
  positive: "#3E7C5A",
  watch: "#B7791F",
  risk: "#B4443C",
} as const;

/** The RGNR8 infinity monogram — the "8" as an endless loop (no external asset). */
export function markSvg(size = 26, color: string = RG.sage): string {
  const w = Math.round((size * 40) / 24);
  return (
    `<svg viewBox="0 0 40 24" height="${size}" width="${w}" role="img" aria-label="RGNR8" ` +
    'style="vertical-align:middle;flex:none">' +
    `<path d="M8 12 C8 5.5 16 5.5 20 12 C24 18.5 32 18.5 32 12 ` +
    `C32 5.5 24 5.5 20 12 C16 18.5 8 18.5 8 12 Z" ` +
    `fill="none" stroke="${color}" stroke-width="3.2" stroke-linecap="round"/>` +
    "</svg>"
  );
}

export const RG_TOKENS_CSS = `:root{
  --rg-forest:${RG.forest}; --rg-ivory:${RG.ivory}; --rg-sage:${RG.sage};
  --rg-ink:${RG.ink}; --rg-ink-2:${RG.ink2}; --rg-muted:${RG.muted};
  --rg-paper:${RG.paper}; --rg-surface:${RG.surface}; --rg-line:${RG.line};
  --rg-accent:${RG.accent}; --rg-accent-deep:${RG.accentDeep};
  --rg-pos:${RG.positive}; --rg-watch:${RG.watch}; --rg-risk:${RG.risk};
  --rg-radius:12px; --rg-pill:999px;
  --rg-shadow:0 1px 2px rgba(14,36,30,.07),0 1px 3px rgba(14,36,30,.05);
  --rg-serif:ui-serif,Georgia,"Times New Roman",serif;
  --rg-sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}`;

export const RG_BASE_CSS = `*{box-sizing:border-box}
body{margin:0;background:var(--rg-surface);color:var(--rg-ink);
  font:400 15px/1.55 var(--rg-sans);-webkit-font-smoothing:antialiased}
h1{font-family:var(--rg-serif)}
.rg-bar{display:flex;align-items:center;justify-content:space-between;gap:16px;
  background:var(--rg-forest);color:var(--rg-ivory);padding:14px 22px}
.rg-lockup{display:inline-flex;align-items:center;gap:11px}
.rg-wordmark{font:600 18px/1 var(--rg-sans);
  letter-spacing:.22em;text-transform:uppercase;color:var(--rg-ivory)}
.rg-wordmark .rg-8{color:var(--rg-sage)}
.rg-ctx{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#A9B7AC;
  font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rg-shell{max-width:960px;margin:0 auto;padding:24px 20px 56px}
.rg-eyebrow{font-size:11px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--rg-muted)}
.rg-badge{display:inline-flex;align-items:center;gap:7px;border-radius:var(--rg-pill);
  padding:4px 12px;font-weight:700;font-size:12px;letter-spacing:.05em;text-transform:uppercase}
.rg-dot{display:inline-block;width:9px;height:9px;border-radius:50%;vertical-align:middle;margin-right:7px}
.rg-foot{color:var(--rg-muted);font-size:12px;margin-top:14px}
:focus-visible{outline:2px solid var(--rg-sage);outline-offset:2px}
/* Shared component layer — the single source for card/table/banner/btn/tile/dot,
   so no renderer redeclares (or drifts from) these. */
.card{background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:var(--rg-radius);
  box-shadow:var(--rg-shadow);padding:20px 22px;margin-bottom:16px}
.muted{color:var(--rg-muted)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.neg{color:var(--rg-risk)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:separate;border-spacing:0;width:100%;background:var(--rg-paper);
  border:1px solid var(--rg-line);border-radius:var(--rg-radius);overflow:hidden;box-shadow:var(--rg-shadow)}
th,td{text-align:left;padding:11px 13px;border-bottom:1px solid var(--rg-line);font-size:13.5px}
thead th{background:var(--rg-ivory);font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-muted)}
tbody tr:last-child td{border-bottom:none}
.btn{background:var(--rg-forest);color:var(--rg-ivory);border:none;border-radius:10px;
  padding:10px 15px;font-weight:700;cursor:pointer;font:inherit}
.btn.sage{background:var(--rg-accent-deep)}
.btn.ghost{background:transparent;color:var(--rg-forest);border:1px solid var(--rg-line)}
.banner{padding:12px 16px;border-radius:var(--rg-radius);margin-bottom:16px;font-weight:700;font-size:13px;border:1px solid transparent}
.banner.good{background:color-mix(in srgb, var(--rg-pos) 14%, var(--rg-paper));
  color:color-mix(in srgb, var(--rg-pos) 78%, black);border-color:color-mix(in srgb, var(--rg-pos) 32%, transparent)}
.banner.warn{background:color-mix(in srgb, var(--rg-risk) 14%, var(--rg-paper));
  color:color-mix(in srgb, var(--rg-risk) 78%, black);border-color:color-mix(in srgb, var(--rg-risk) 32%, transparent)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:8px}
.tile{background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:12px;padding:15px 16px}
.tile .k{color:var(--rg-muted);font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.tile .v{font-size:23px;font-weight:800;margin-top:6px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.pill{display:inline-flex;align-items:center;gap:7px;border-radius:var(--rg-pill);padding:4px 12px;
  font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.04em}`;

export const RG_THEME_CSS = RG_TOKENS_CSS + "\n" + RG_BASE_CSS;

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Semantic status → brand color. */
export function rgStatusColor(level: string): string {
  const k = level.toUpperCase().replace(/ /g, "_");
  if (k === "STABLE" || k === "MATCH" || k === "OK") return RG.positive;
  if (k === "WATCH") return RG.watch;
  return RG.risk;
}

/** The forest RGNR8 top bar: infinity mark + wordmark left, context right. */
export function brandBar(context: string): string {
  return (
    '<header class="rg-bar">' +
    '<span class="rg-lockup">' +
    markSvg(22, RG.ivory) +
    '<span class="rg-wordmark">RGNR<span class="rg-8">8</span></span>' +
    "</span>" +
    `<span class="rg-ctx">${esc(context)}</span>` +
    "</header>"
  );
}
