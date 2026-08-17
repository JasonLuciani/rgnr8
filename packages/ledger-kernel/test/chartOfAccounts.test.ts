import { test } from "node:test";
import assert from "node:assert/strict";

import {
  AccountSubtype,
  AccountType,
  ChartOfAccounts,
  accountTypeOfSubtype,
  asAccountId,
  USD,
} from "../src/index.js";
import type { Account } from "../src/index.js";

function acct(p: Partial<Account> & { code: string; type: AccountType }): Account {
  return {
    id: asAccountId(p.id ?? p.code),
    code: p.code,
    name: p.name ?? p.code,
    type: p.type,
    currency: USD,
    ...(p.subtype !== undefined ? { subtype: p.subtype } : {}),
    ...(p.parentId !== undefined ? { parentId: p.parentId } : {}),
  } as Account;
}

test("every subtype maps to exactly one type", () => {
  for (const st of Object.values(AccountSubtype)) {
    const t = accountTypeOfSubtype(st);
    assert.ok(Object.values(AccountType).includes(t));
  }
  assert.equal(accountTypeOfSubtype(AccountSubtype.BANK), AccountType.ASSET);
  assert.equal(accountTypeOfSubtype(AccountSubtype.ACCOUNTS_PAYABLE), AccountType.LIABILITY);
  assert.equal(accountTypeOfSubtype(AccountSubtype.COST_OF_GOODS_SOLD), AccountType.EXPENSE);
});

test("a subtype that doesn't match the type is rejected", () => {
  assert.throws(
    () =>
      new ChartOfAccounts([
        acct({ code: "1000", type: AccountType.LIABILITY, subtype: AccountSubtype.BANK }),
      ]),
    /does not belong to type/,
  );
});

test("sub-accounts must reference a known parent of the same type", () => {
  // unknown parent
  assert.throws(
    () =>
      new ChartOfAccounts([
        acct({ code: "1001", type: AccountType.ASSET, parentId: asAccountId("nope") }),
      ]),
    /unknown parentId/,
  );
  // wrong-type parent
  const parent = acct({ code: "2000", type: AccountType.LIABILITY });
  assert.throws(
    () =>
      new ChartOfAccounts([
        parent,
        acct({ code: "1000", type: AccountType.ASSET, parentId: parent.id }),
      ]),
    /must match parent type/,
  );
});

test("hierarchy: children / descendants / subtree roll up", () => {
  const bank = acct({ code: "1000", type: AccountType.ASSET, subtype: AccountSubtype.BANK });
  const checking = acct({ code: "1010", type: AccountType.ASSET, parentId: bank.id });
  const savings = acct({ code: "1020", type: AccountType.ASSET, parentId: bank.id });
  const subChecking = acct({ code: "1011", type: AccountType.ASSET, parentId: checking.id });
  const coa = new ChartOfAccounts([bank, checking, savings, subChecking]);

  assert.deepEqual(
    coa.children(bank.id).map((a) => a.code),
    ["1010", "1020"],
  );
  assert.deepEqual(
    coa.descendants(bank.id).map((a) => a.code),
    ["1010", "1011", "1020"],
  );
  assert.deepEqual(
    coa.subtree(bank.id).map((a) => a.code),
    ["1000", "1010", "1011", "1020"],
  );
});

test("accounts are active by default and can be deactivated (kept for history)", () => {
  const a = acct({ code: "5000", type: AccountType.EXPENSE });
  const coa = new ChartOfAccounts([a]);
  assert.equal(coa.get(a.id)?.active, true);
  assert.equal(coa.listActive().length, 1);

  coa.deactivate(a.id);
  assert.equal(coa.get(a.id)?.active, false);
  assert.equal(coa.listActive().length, 0);
  assert.equal(coa.list().length, 1); // still there, just inactive

  coa.activate(a.id);
  assert.equal(coa.listActive().length, 1);
});
