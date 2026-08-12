import { normalizeQboAccountType, parseAmount } from "./csv.js";
import type {
  QboBankBalance,
  QboOpenBill,
  QboOpenInvoice,
  QboOverlaySnapshot,
} from "./overlay.js";
import type {
  QboAccount,
  QboExport,
  QboJournalEntry,
  QboJournalLine,
  QboTrialBalanceRow,
} from "./types.js";

/**
 * QuickBooks Online **API** adapter — pulls the same data the CSV adapters parse
 * from files, but straight from Intuit's REST API, emitting the identical
 * `QboExport` so it feeds the same import + parallel-close pipeline. Everything
 * goes over an injected `QboHttpClient` (the seam pattern used everywhere in
 * this codebase), so the request-building + report-parsing is unit-tested with
 * fixture JSON and no network. Going live against a real company is just
 * supplying a `fetch`-backed client + an OAuth access token + the realm id.
 *
 * Scope note: `fetchAccounts` (chart) and `fetchTrialBalance` (QBO's reported
 * TB — the authoritative side of the parallel-close compare) are complete.
 * `fetchJournalEntries` maps QBO `JournalEntry` objects (manual journals); a
 * faithful full-GL import across all transaction types (invoices, bills,
 * payments…) needs the GeneralLedger report and is the next step — until then,
 * pair the API's trial balance with the ledger you're migrating into.
 */

export interface QboHttpResponse {
  readonly status: number;
  readonly json: unknown;
}

export interface QboHttpClient {
  /** GET a fully-formed QBO API URL with the given headers. Wrap `fetch`. */
  get(url: string, headers: Readonly<Record<string, string>>): Promise<QboHttpResponse>;
}

export interface QboApiOptions {
  readonly realmId: string;
  readonly accessToken: string;
  readonly baseUrl?: string; // sandbox: https://sandbox-quickbooks.api.intuit.com
  readonly minorVersion?: number;
}

export class QboApiError extends Error {
  override readonly name = "QboApiError";
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

// --- report row helpers ------------------------------------------------------

interface ColData {
  readonly value?: string;
  readonly id?: string;
}
interface ReportRow {
  readonly ColData?: readonly ColData[];
  readonly Header?: { readonly ColData?: readonly ColData[] };
  readonly Rows?: { readonly Row?: readonly ReportRow[] };
  readonly Summary?: { readonly ColData?: readonly ColData[] };
  readonly type?: string;
}

/** Depth-first collect every data row's ColData in a QBO report (skips the
 * section Summary/total rows). */
function collectDataRows(rows: readonly ReportRow[] | undefined, out: ColData[][]): void {
  if (!rows) return;
  for (const r of rows) {
    if (r.ColData && r.ColData.length > 0) out.push([...r.ColData]);
    if (r.Rows?.Row) collectDataRows(r.Rows.Row, out);
  }
}

function num(s: string | undefined): string | undefined {
  if (s === undefined) return undefined;
  const t = s.trim();
  if (t === "" || t === "0" || t === "0.00") return t === "" ? undefined : t;
  return t;
}

// --- GeneralLedger report → balanced journal entries -------------------------

interface ReportColumn {
  readonly ColTitle?: string;
}

function colIdx(columns: readonly ReportColumn[], ...names: string[]): number {
  const lower = columns.map((c) => (c.ColTitle ?? "").toLowerCase());
  for (const name of names) {
    const idx = lower.findIndex((t) => t.includes(name));
    if (idx !== -1) return idx;
  }
  return -1;
}

/**
 * Reconstruct balanced double-entry journal entries from QBO's **GeneralLedger**
 * report. The GL is organized *by account*: each transaction appears once under
 * every account it touches, with that account's signed amount (debit positive,
 * credit negative). We walk the account sections, tag every transaction row with
 * its section's account, and **group rows across sections by transaction id** —
 * yielding one entry per transaction whose lines sum to zero. This captures all
 * transaction types (invoices, bills, payments, deposits…), not just manual
 * journal entries. Any entry that doesn't balance is caught downstream by
 * `toPostCommands` (dropped to an issue), not silently posted.
 */
export function parseGeneralLedger(report: Record<string, unknown>): QboJournalEntry[] {
  const columns = ((report["Columns"] as { Column?: ReportColumn[] } | undefined)?.Column) ?? [];
  const dateCol = colIdx(columns, "date");
  const typeCol = colIdx(columns, "transaction type", "type");
  const numCol = colIdx(columns, "num", "number");
  const amountCol = colIdx(columns, "amount");

  const byTxn = new Map<string, { date: string; lines: QboJournalLine[] }>();

  const walk = (rows: readonly ReportRow[] | undefined, account: string): void => {
    if (!rows) return;
    for (const r of rows) {
      if (r.ColData && r.ColData.length > 0) {
        recordRow(r.ColData, account);
      }
      if (r.Rows?.Row) {
        // a section carries its account in its own Header; else inherit
        const sectionAccount = (r.Header?.ColData?.[0]?.value ?? "").trim() || account;
        walk(r.Rows.Row, sectionAccount);
      } else if (r.Header?.ColData?.[0]?.value) {
        // header-only section with inline rows already handled above
      }
    }
  };

  const recordRow = (cols: readonly ColData[], account: string): void => {
    if (account === "" || amountCol < 0) return;
    const amountRaw = parseAmount(cols[amountCol]?.value);
    if (amountRaw === undefined) return; // subtotal / blank rows
    const date = (dateCol >= 0 ? cols[dateCol]?.value : "")?.trim() ?? "";
    if (date === "") return; // section subtotal rows have no date
    // transaction id: prefer an id on any ColData cell, else synthesize
    const idCell = cols.find((c) => c.id !== undefined)?.id;
    const type = (typeCol >= 0 ? cols[typeCol]?.value : "")?.trim() ?? "";
    const numv = (numCol >= 0 ? cols[numCol]?.value : "")?.trim() ?? "";
    const txnId = idCell ?? `${type}|${numv}|${date}`;

    const negative = amountRaw.startsWith("-");
    const magnitude = negative ? amountRaw.slice(1) : amountRaw;
    const line: QboJournalLine = negative
      ? { account, credit: magnitude }
      : { account, debit: magnitude };

    const entry = byTxn.get(txnId) ?? { date, lines: [] };
    entry.lines.push(line);
    byTxn.set(txnId, entry);
  };

  walk((report["Rows"] as { Row?: ReportRow[] } | undefined)?.Row, "");

  const entries: QboJournalEntry[] = [];
  for (const [id, e] of byTxn) {
    if (e.lines.length > 0) entries.push({ id: `GL-${id}`, date: e.date, lines: e.lines });
  }
  return entries;
}

export class QboApiClient {
  private readonly http: QboHttpClient;
  private readonly realm: string;
  private readonly token: string;
  private readonly base: string;
  private readonly mv: number;

  constructor(http: QboHttpClient, opts: QboApiOptions) {
    this.http = http;
    this.realm = opts.realmId;
    this.token = opts.accessToken;
    this.base = opts.baseUrl ?? "https://quickbooks.api.intuit.com";
    this.mv = opts.minorVersion ?? 70;
  }

  private headers(): Record<string, string> {
    return { Authorization: `Bearer ${this.token}`, Accept: "application/json" };
  }

  private async getJson(path: string): Promise<Record<string, unknown>> {
    const url = `${this.base}/v3/company/${this.realm}/${path}${path.includes("?") ? "&" : "?"}minorversion=${this.mv}`;
    const res = await this.http.get(url, this.headers());
    if (res.status < 200 || res.status >= 300) {
      throw new QboApiError(res.status, `QBO API ${path} → HTTP ${res.status}`);
    }
    return (res.json ?? {}) as Record<string, unknown>;
  }

  private async query(statement: string): Promise<Record<string, unknown>> {
    const body = await this.getJson(`query?query=${encodeURIComponent(statement)}`);
    return (body["QueryResponse"] ?? {}) as Record<string, unknown>;
  }

  async fetchAccounts(): Promise<QboAccount[]> {
    const qr = await this.query("select * from Account maxresults 1000");
    const accounts = (qr["Account"] as Record<string, unknown>[] | undefined) ?? [];
    return accounts.map((a) => {
      const name = String(a["Name"] ?? "");
      const acctNum = a["AcctNum"] !== undefined ? String(a["AcctNum"]) : "";
      const typeRaw = String(a["AccountType"] ?? a["Classification"] ?? "Expense");
      const acct: QboAccount = { name, type: normalizeQboAccountType(typeRaw) };
      return acctNum !== "" ? { ...acct, acctNum } : acct;
    });
  }

  async fetchTrialBalance(asOf?: string): Promise<QboTrialBalanceRow[]> {
    const q = asOf ? `reports/TrialBalance?end_date=${asOf}` : "reports/TrialBalance";
    const report = await this.getJson(q);
    const columns = ((report["Columns"] as { Column?: { ColTitle?: string }[] } | undefined)?.Column) ?? [];
    const debitCol = columns.findIndex((c) => (c.ColTitle ?? "").toLowerCase().includes("debit"));
    const creditCol = columns.findIndex((c) => (c.ColTitle ?? "").toLowerCase().includes("credit"));
    const rows: ColData[][] = [];
    collectDataRows((report["Rows"] as { Row?: ReportRow[] } | undefined)?.Row, rows);

    const out: QboTrialBalanceRow[] = [];
    for (const cols of rows) {
      const account = (cols[0]?.value ?? "").trim();
      if (account === "" || /^total\b/i.test(account)) continue;
      const debit = debitCol >= 0 ? num(cols[debitCol]?.value) : undefined;
      const credit = creditCol >= 0 ? num(cols[creditCol]?.value) : undefined;
      if (debit === undefined && credit === undefined) continue;
      out.push({
        account,
        ...(debit !== undefined ? { debit } : {}),
        ...(credit !== undefined ? { credit } : {}),
      });
    }
    return out;
  }

  /** Manual `JournalEntry` objects → journal entries. (Invoices/bills/payments
   * are NOT included — those need the GeneralLedger report; see the scope note.) */
  async fetchJournalEntries(): Promise<QboJournalEntry[]> {
    const qr = await this.query("select * from JournalEntry maxresults 1000");
    const jes = (qr["JournalEntry"] as Record<string, unknown>[] | undefined) ?? [];
    const entries: QboJournalEntry[] = [];
    for (const je of jes) {
      const id = String(je["Id"] ?? "");
      const date = String(je["TxnDate"] ?? "");
      const rawLines = (je["Line"] as Record<string, unknown>[] | undefined) ?? [];
      const lines: QboJournalLine[] = [];
      for (const l of rawLines) {
        const detail = l["JournalEntryLineDetail"] as Record<string, unknown> | undefined;
        if (!detail) continue; // skip description-only lines
        const account = String((detail["AccountRef"] as { name?: string } | undefined)?.name ?? "");
        const amount = String(l["Amount"] ?? "0");
        const posting = String(detail["PostingType"] ?? "");
        if (account === "" || amount === "0") continue;
        if (posting === "Debit") lines.push({ account, debit: amount });
        else if (posting === "Credit") lines.push({ account, credit: amount });
      }
      if (lines.length > 0) entries.push({ id: `JE-${id}`, date, lines });
    }
    return entries;
  }

  /**
   * Fetch the **GeneralLedger** report and reconstruct balanced journal entries
   * across *all* transaction types (see `parseGeneralLedger`). This is the
   * faithful full-import path.
   */
  async fetchGeneralLedger(startDate: string, endDate: string): Promise<QboJournalEntry[]> {
    const report = await this.getJson(
      `reports/GeneralLedger?start_date=${startDate}&end_date=${endDate}`,
    );
    return parseGeneralLedger(report);
  }

  /** Assemble a `QboExport` from the **GeneralLedger** (full transaction history)
   * plus the chart and reported trial balance — a faithful company migration. */
  async fetchExportViaGeneralLedger(
    startDate: string,
    endDate: string,
  ): Promise<QboExport> {
    const [accounts, reportedTrialBalance, entries] = await Promise.all([
      this.fetchAccounts(),
      this.fetchTrialBalance(endDate),
      this.fetchGeneralLedger(startDate, endDate),
    ]);
    return { accounts, entries, reportedTrialBalance };
  }

  /** Assemble a `QboExport`. Accounts + reported trial balance are complete;
   * `entries` covers manual journal entries only (see scope note). For a full
   * migration use `fetchExportViaGeneralLedger`. */
  async fetchExport(asOf?: string): Promise<QboExport> {
    const [accounts, reportedTrialBalance, entries] = await Promise.all([
      this.fetchAccounts(),
      this.fetchTrialBalance(asOf),
      this.fetchJournalEntries(),
    ]);
    return { accounts, entries, reportedTrialBalance };
  }

  // --- overlay path (Stage 1: RGNR8 on top of QBO) ---------------------------

  /** Bank-type accounts with their QBO-reported `CurrentBalance` — the opening
   * cash for an overlay forecast. */
  async fetchBankBalances(): Promise<QboBankBalance[]> {
    const qr = await this.query(
      "select * from Account where AccountType = 'Bank' maxresults 1000",
    );
    const accounts = (qr["Account"] as Record<string, unknown>[] | undefined) ?? [];
    return accounts.map((a) => {
      const bal =
        a["CurrentBalance"] !== undefined ? String(a["CurrentBalance"]) : "0";
      const row: QboBankBalance = {
        id: String(a["Id"] ?? ""),
        name: String(a["Name"] ?? ""),
        currentBalance: bal,
      };
      const ccy = (a["CurrencyRef"] as { value?: string } | undefined)?.value;
      return ccy ? { ...row, currency: ccy } : row;
    });
  }

  /** Open customer invoices (`Balance > 0`) → forecast AR inflows. */
  async fetchOpenInvoices(): Promise<QboOpenInvoice[]> {
    const qr = await this.query(
      "select * from Invoice where Balance > '0' maxresults 1000",
    );
    const invoices = (qr["Invoice"] as Record<string, unknown>[] | undefined) ?? [];
    return invoices.map((inv) => {
      const ref = inv["CustomerRef"] as { value?: string; name?: string } | undefined;
      const issue = String(inv["TxnDate"] ?? "");
      const row: QboOpenInvoice = {
        id: `INV-${String(inv["Id"] ?? "")}`,
        customerId: String(ref?.value ?? "unknown"),
        issueDate: issue,
        dueDate: String(inv["DueDate"] ?? issue),
        balance: String(inv["Balance"] ?? "0"),
      };
      return ref?.name ? { ...row, customerName: ref.name } : row;
    });
  }

  /** Open vendor bills (`Balance > 0`) → forecast AP outflows. */
  async fetchOpenBills(): Promise<QboOpenBill[]> {
    const qr = await this.query(
      "select * from Bill where Balance > '0' maxresults 1000",
    );
    const bills = (qr["Bill"] as Record<string, unknown>[] | undefined) ?? [];
    return bills.map((bill) => {
      const ref = bill["VendorRef"] as { value?: string; name?: string } | undefined;
      const txn = String(bill["TxnDate"] ?? "");
      const row: QboOpenBill = {
        id: `BILL-${String(bill["Id"] ?? "")}`,
        vendorId: String(ref?.value ?? "unknown"),
        txnDate: txn,
        dueDate: String(bill["DueDate"] ?? txn),
        balance: String(bill["Balance"] ?? "0"),
      };
      return ref?.name ? { ...row, vendorName: ref.name } : row;
    });
  }

  /** One live read of everything an overlay cash forecast needs: bank balances,
   * open AR, open AP — mapped to a `forecast-inputs/1` DTO by
   * `buildOverlayForecastInputs`. `asOf` is the snapshot date (defaults caller-side). */
  async fetchOverlaySnapshot(asOf: string): Promise<QboOverlaySnapshot> {
    const [bankAccounts, openInvoices, openBills] = await Promise.all([
      this.fetchBankBalances(),
      this.fetchOpenInvoices(),
      this.fetchOpenBills(),
    ]);
    return { asOf, bankAccounts, openInvoices, openBills };
  }
}
