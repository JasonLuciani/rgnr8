import { test } from "node:test";
import assert from "node:assert/strict";
import { RG, RG_THEME_CSS, brandBar, rgStatusColor } from "../src/brand.js";

test("brand palette is the RGNR8 guide (forest green, ivory, sage + muted status)", () => {
  assert.equal(RG.forest, "#0E241E");
  assert.equal(RG.ivory, "#F2EFE6");
  assert.equal(RG.sage, "#5E7F63");
  assert.equal(RG.ink, "#0E241E");
  assert.equal(RG.accent, "#5E7F63");
  assert.equal(RG.positive, "#3E7C5A");
  assert.equal(RG.watch, "#B7791F");
  assert.equal(RG.risk, "#B4443C");
});

test("brandBar renders the wordmark with the accent 8 and an uppercased context", () => {
  const bar = brandBar("Parallel close");
  assert.match(bar, /class="rg-wordmark"/);
  assert.match(bar, /RGNR<span class="rg-8">8<\/span>/);
  assert.match(bar, /Parallel close/);
});

test("rgStatusColor maps semantic levels to brand colors", () => {
  assert.equal(rgStatusColor("STABLE"), RG.positive);
  assert.equal(rgStatusColor("match"), RG.positive);
  assert.equal(rgStatusColor("WATCH"), RG.watch);
  assert.equal(rgStatusColor("AT_RISK"), RG.risk);
});

test("theme CSS is self-contained — no external URLs", () => {
  assert.ok(!RG_THEME_CSS.includes("http://"));
  assert.ok(!RG_THEME_CSS.includes("https://"));
  assert.ok(RG_THEME_CSS.includes("--rg-sage:#5E7F63"));
});

test("brandBar escapes its context", () => {
  assert.match(brandBar("<script>"), /&lt;script&gt;/);
});
