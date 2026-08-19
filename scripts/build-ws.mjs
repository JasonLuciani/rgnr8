#!/usr/bin/env node
// Build every workspace package that has a `build` script, in dependency order.
//
// The packages resolve each other through their built `dist/` (each `@rgnr8/*`
// package's `types`/`main` point at `dist`), and there are no TypeScript project
// references, so a package cannot compile until the packages it imports have
// already been built. `npm run build --workspaces` runs in directory order, not
// dependency order, so on a fresh checkout (nothing pre-built) it fails with
// "Cannot find module '@rgnr8/ledger-kernel'". This script topologically sorts
// the workspaces by their intra-repo `@rgnr8/*` dependencies and builds them in
// that order, so a clean checkout builds deterministically.

import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const pkgsDir = join(root, "packages");

/** name -> { name, hasBuild, deps: Set<string> (intra-repo only) } */
const graph = new Map();

for (const dir of readdirSync(pkgsDir)) {
  const pjPath = join(pkgsDir, dir, "package.json");
  if (!existsSync(pjPath)) continue;
  const pj = JSON.parse(readFileSync(pjPath, "utf8"));
  if (!pj.name) continue;
  const deps = new Set(
    Object.keys({ ...pj.dependencies, ...pj.devDependencies }).filter((d) =>
      d.startsWith("@rgnr8/"),
    ),
  );
  graph.set(pj.name, { name: pj.name, hasBuild: Boolean(pj.scripts?.build), deps });
}

// Keep only intra-repo edges (a dep that isn't its own workspace is external).
for (const node of graph.values()) {
  node.deps = new Set([...node.deps].filter((d) => graph.has(d)));
}

// Kahn's algorithm — deterministic order (sort ready set by name for stability).
const order = [];
const remaining = new Map([...graph].map(([n, v]) => [n, new Set(v.deps)]));
while (remaining.size > 0) {
  const ready = [...remaining.entries()]
    .filter(([, deps]) => deps.size === 0)
    .map(([n]) => n)
    .sort();
  if (ready.length === 0) {
    throw new Error(`dependency cycle among: ${[...remaining.keys()].join(", ")}`);
  }
  for (const name of ready) {
    order.push(name);
    remaining.delete(name);
  }
  for (const deps of remaining.values()) {
    for (const done of ready) deps.delete(done);
  }
}

const toBuild = order.filter((n) => graph.get(n).hasBuild);
console.log(`Building ${toBuild.length} packages in dependency order:\n  ${toBuild.join("\n  ")}`);

for (const name of toBuild) {
  console.log(`\n=== build ${name} ===`);
  execFileSync("npm", ["run", "build", "-w", name], { stdio: "inherit", cwd: root });
}
console.log("\nAll packages built.");
