import { test } from "node:test";
import assert from "node:assert/strict";
import {
  QboApiClient,
  buildOverlayForecastInputs,
  type QboHttpClient,
  type QboHttpResponse,
  type QboOverlaySnapshot,
} from "../src/index.js";

// --- a fake QBO API for the overlay queries --------------------------------

const BANK_JSON = {
  QueryResponse: {
    Account: [
      { Id: "35", Name: "Checking", AccountType: "Bank", CurrentBalance: 48210.55 },
      { Id: "36", Name: "Savings", AccountType: "Bank", CurrentBalance: 15000.0 },
    ],
  },
};

const INVOICE_JSON = {
  QueryResponse: {
    Invoice: [
      {
        Id: "101",
        TxnDate: "2026-07-20",
        DueDate: "2026-08-19",
        Balance: 18000.0,
        CustomerRef: { value: "C-7", name: "Northwind LLC" },
      },
      {
        Id: "102",
        TxnDate: "2026-08-01",
        DueDate: "2026-08-31",
        Balance: 4200.5,
        CustomerRef: { value: "C-9", name: "Contoso" },
      },
    ],
  },
};

const BILL_JSON = {
  QueryResponse: {
    Bill: [
      {
        Id: "201",
        TxnDate: "2026-07-15",
        DueDate: "2026-08-14",
        Balance: 6000.0,
        VendorRef: { value: "V-3", name: "Acme Supplies" },
      },
    ],
  },
};

function fakeQbo(): QboHttpClient {
  return {
    async get(url: string): Promise<QboHttpResponse> {
      const u = decodeURIComponent(url);
      if (u.includes("from Account")) return { status: 200, json: BANK_JSON };
      if (u.includes("from Invoice")) return { status: 200, json: INVOICE_JSON };
      if (u.includes("from Bill")) return { status: 200, json: BILL_JSON };
      return { status: 404, json: {} };
    },
  };
}

const OPTS = { realmId: "9130347", accessToken: "tok-abc" };

test("fetchOverlaySnapshot pulls bank balances + open AR + open AP", async () => {
  const client = new QboApiClient(fakeQbo(), OPTS);
  const snap = await client.fetchOverlaySnapshot("2026-08-31");
  assert.equal(snap.bankAccounts.length, 2);
  assert.equal(snap.openInvoices.length, 2);
  assert.equal(snap.openBills.length, 1);
  assert.equal(snap.openInvoices[0]?.id, "INV-101");
  assert.equal(snap.openInvoices[0]?.customerId, "C-7");
  assert.equal(snap.openInvoices[0]?.customerName, "Northwind LLC");
  assert.equal(snap.openBills[0]?.id, "BILL-201");
  assert.equal(snap.openBills[0]?.vendorId, "V-3");
});

test("buildOverlayForecastInputs emits a forecast-inputs/1 DTO with opening = sum of bank balances", () => {
  const snapshot: QboOverlaySnapshot = {
    asOf: "2026-08-31",
    bankAccounts: [
      { id: "35", name: "Checking", currentBalance: "48210.55" },
      { id: "36", name: "Savings", currentBalance: "15000.00" },
    ],
    openInvoices: [
      { id: "INV-101", customerId: "C-7", issueDate: "2026-07-20", dueDate: "2026-08-19", balance: "18000.00" },
    ],
    openBills: [
      { id: "BILL-201", vendorId: "V-3", txnDate: "2026-07-15", dueDate: "2026-08-14", balance: "6000.00" },
    ],
  };
  const { dto } = buildOverlayForecastInputs(snapshot);
  assert.equal(dto.contract, "forecast-inputs/1");
  assert.equal(dto.currency, "USD");
  // 48210.55 + 15000.00 = 63210.55 → 6321055 minor units
  assert.deepEqual(dto.opening.available, { minor: 6321055, currency: "USD" });
  assert.equal(dto.opening.as_of, "2026-08-31");
  assert.equal(dto.opening.verified, false); // reported balances, not reconciled
  assert.equal(dto.invoices.length, 1);
  assert.deepEqual(dto.invoices[0]?.open_amount, { minor: 1800000, currency: "USD" });
  assert.equal(dto.invoices[0]?.status, "OPEN");
  assert.equal(dto.bills.length, 1);
  assert.deepEqual(dto.bills[0]?.amount, { minor: 600000, currency: "USD" });
  assert.equal(dto.bills[0]?.scheduled_date, null);
});

test("overlay drops zero/unparseable-balance rows and notes them", () => {
  const snapshot: QboOverlaySnapshot = {
    asOf: "2026-08-31",
    bankAccounts: [{ id: "35", name: "Checking", currentBalance: "1000.00" }],
    openInvoices: [
      { id: "INV-1", customerId: "C-1", issueDate: "2026-08-01", dueDate: "2026-08-31", balance: "0.00" },
      { id: "INV-2", customerId: "C-2", issueDate: "2026-08-01", dueDate: "2026-08-31", balance: "500.00" },
    ],
    openBills: [{ id: "BILL-1", vendorId: "V-1", txnDate: "2026-08-01", dueDate: "2026-08-31", balance: "junk" }],
  };
  const { dto, notes } = buildOverlayForecastInputs(snapshot);
  assert.equal(dto.invoices.length, 1);
  assert.equal(dto.invoices[0]?.id, "INV-2");
  assert.equal(dto.bills.length, 0);
  assert.ok(notes.some((n) => n.includes("1 invoice(s) dropped")));
  assert.ok(notes.some((n) => n.includes("1 bill(s) dropped")));
});

test("openingVerified flag marks the opening position verified", () => {
  const snapshot: QboOverlaySnapshot = {
    asOf: "2026-08-31",
    bankAccounts: [{ id: "35", name: "Checking", currentBalance: "1000.00" }],
    openInvoices: [],
    openBills: [],
  };
  const { dto } = buildOverlayForecastInputs(snapshot, { openingVerified: true });
  assert.equal(dto.opening.verified, true);
});

test("due date falls back to issue/txn date when QBO omits it", () => {
  const snapshot: QboOverlaySnapshot = {
    asOf: "2026-08-31",
    bankAccounts: [],
    openInvoices: [{ id: "INV-1", customerId: "C-1", issueDate: "2026-08-10", dueDate: "", balance: "100.00" }],
    openBills: [{ id: "BILL-1", vendorId: "V-1", txnDate: "2026-08-05", dueDate: "", balance: "50.00" }],
  };
  const { dto } = buildOverlayForecastInputs(snapshot);
  assert.equal(dto.invoices[0]?.due_date, "2026-08-10");
  assert.equal(dto.bills[0]?.due_date, "2026-08-05");
});
