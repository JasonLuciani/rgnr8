import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { ledgerMigrations } from "../src/migrations.js";

/**
 * Applied migrations are history. Production refuses to boot when an applied
 * migration's SQL no longer matches what was recorded (MigrationDriftError),
 * and that refusal is only discovered on deploy, after the merge. This test
 * moves the discovery to CI: every migration's SQL is pinned here by hash.
 *
 * Adding a migration: append its version and hash below. Changing an existing
 * one: don't. Write a new migration instead (see SETTINGS_MIRROR_DDL for how
 * columns are added to a table that already exists).
 */
const APPLIED: Record<number, string> = {
  1: "4a26e49cfae6a4c6b139161da33cfea88f1d8a16e894bca16421d4366af8d746",
  2: "0f681214b8f93d889d930d6fc28c0a0c9e583bee77a4796a083dd7b96651caf9",
  3: "e9804c37477cf135aac71ddb632e12c74d5f07fa76742a723d70409d840fa842",
  4: "4307a7f3c0b87dd565c70b3865b5d813c3d70538ebf7ff446f6310770e3429d9",
  5: "fb4af7f175076ecfdf3ea290f16b40e3decc82156e3f687f57b21574c49a56a6",
  6: "5c2e2e4346e0db3c0c3eacd982cf07b78c24f3098518581bdc7df351acfcbc2a",
  7: "0ca00fc94004c1e5780825fd5f693747907c97a1233b5fa5f723b6ed424901bf",
  8: "679d2eb09cb93b9b01c171d5e7c749185e8b5e6b72b91aa7a617116c8bdf6123",
  9: "e4f7d361db886d1a13884d57b8fe5dc9ffc3ed78143b8e744deb97421052ef1d",
  10: "de384263ef8e9c4ab1a7b7f45f569fff5c5bb5c2fc49c32363800efee29cfa9b",
  11: "7ff149cd38ea60ddeebc69bb227627aab68ae65263a7107f64489b767c895c7c",
  12: "518d197a6c3768b630df5beca09d5e4524e8c5ca9bfb8620596b5c01e5b1f02b",
  13: "973143f765d532d2b84e4c7125091ea87bfbc26a5ecf28b489ccbfc2b839e92a",
  14: "b094c2cb214912f012b9a5b42b606a7a66c98045f924ec6b41e92966ed6e51c5",
  15: "5093f2b52cf235b736f4cde9b54f77c82f754ad004f84615501466febae81a60",
  16: "45c237c0b020b5ecabfec0fe5fe7ecb27274bd98e1a06a3dc4da24aaa1dc2d8c",
  17: "8d02dabf449c19f87330606a70db2490bea67bb8b42859dab292c67500fd3f9e",
  18: "6562a8055ef9e94350bb61345b696fa2b966d08b1ceca667f0b0a2a26e05cdbd",
  19: "e48898306062da18b3663550e9d24461ccc5f54c36a7282864b918b7768ed49f",
  20: "f3aefb5b14e1c987a4dc587bd65ab0f2d5ef903107f684094f24e56dce59b0f1",
  21: "c213c9c0442279d1a874ee6cd39bdb54975bd91667c34e307202d0a9cdc149df",
  22: "f08dcdabab240eef78b2263c5d8289e1fc6ec9513b558378a5504d31bbfc5700",
  23: "a79ff634e504fca3cb48188f56a75c9e0d2ca1fba6800c8235b57a7b907d2757",
  24: "198d202f92fee6d332fff31b9511a593ae11e077904c60b4535a982fe9de1bec",
  25: "a8b45d11db586dd608e36e572029702598603d39d8e3d5be7661be7eff91a19a",
  26: "09863bc49fd51510493646c237bdeba94d0e411bc8dfae6a4d2af788f3a140cd",
  27: "abc849b79b8caa521c55740acee419bc4248033f948d4912ee1ac01df6ff7237",
  28: "538a25e29ad685e366274172946ff715c95c826c0d0d22c6723852151bb54ee5",
  100: "571297610009ceaed4746f00ffb546974f2101b6a51040e8a42a6049150c7ee5",
  101: "ce8b0eccb676b79e5446e3aacdd7b76725bbcac86ae09ac1fc150448eb9f95c8",
  102: "5446042c161a96fba6b6291074a83e9e5ac3127bd38d9d49d98c3c616932c523",
  103: "cd493e26cacf0440251b260b1ac0d04867f753486f9418c9cc376398f2cd2e7a",
};

const sha = (sql: string): string => createHash("sha256").update(sql).digest("hex");

test("no applied migration has been edited", () => {
  const now = new Map(ledgerMigrations({ enforceRls: true }).map((m) => [m.version, sha(m.sql)]));
  for (const [version, hash] of Object.entries(APPLIED)) {
    assert.equal(
      now.get(Number(version)), hash,
      `migration ${version} changed. Production will refuse to boot (MigrationDriftError). `
      + "Restore its SQL and add a new migration instead.",
    );
  }
});

test("every migration is pinned, so a new one can't ship unreviewed", () => {
  const unpinned = ledgerMigrations({ enforceRls: true })
    .map((m) => m.version).filter((v) => !(v in APPLIED));
  assert.deepEqual(unpinned, [], `pin the hash of new migration(s) ${unpinned.join(", ")} in this file`);
});
