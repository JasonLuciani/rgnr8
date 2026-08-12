import type {
  QboAccount,
  QboAccountType,
  QboJournalEntry,
  QboJournalLine,
  QboTrialBalanceRow,
  QboExport,
} from "./types.js";

/**
 * Concrete adapters that turn real QuickBooks CSV exports into the normalized
 * `QboExport` DTO the importer/comparer work off. QuickBooks Online exports
 * reports as CSV; the three that matter for a migration are the **Journal**
 * report (debit/credit by line, grouped by transaction), the **Trial Balance**,
 * and the **Chart of Accounts**. Everything here is pure and deterministic — no
 * network, no deps — so it's fully unit-testable.
 */

// --- a minimal RFC-4180-ish CSV parser --------------------------------------

/** Parse CSV text into rows of string cells. Handles quoted fields, escaped
 * quotes (""), and commas/newlines inside quotes. Skips a trailing blank line. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let i = 0;
  const n = text.length;
  const pushField = () => {
    row.push(field);
    field = "";
  };
  const pushRow = () => {
    pushField();
    rows.push(row);
    row = [];
  };
  while (i < n) {
    const c = text[i] as string;
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i++;
        continue;
      }
      field += c;
      i++;
      continue;
    }
    if (c === '"') {
      inQuotes = true;
      i++;
      continue;
    }
    if (c === ",") {
      pushField();
      i++;
      continue;
    }
    if (c === "\r") {
      i++;
      continue;
    }
    if (c === "\n") {
      pushRow();
      i++;
      continue;
    }
    field += c;
    i++;
  }
  // last field/row if the file didn't end with a newline
  if (field.length > 0 || row.length > 0) pushRow();
  // drop fully-empty rows (all cells blank)
  return rows.filter((r) => r.some((cell) => cell.trim() !== ""));
}

/** Parse a QBO money cell ("1,234.56", "$1,234.56", "(50.00)", "") into a
 * positive decimal string, or undefined if blank. Parentheses/leading minus
 * yield a negative decimal string. */
export function parseAmount(raw: string | undefined): string | undefined {
  if (raw === undefined) return undefined;
  let s = raw.trim();
  if (s === "" || s === "-") return undefined;
  let negative = false;
  if (s.startsWith("(") && s.endsWith(")")) {
    negative = true;
    s = s.slice(1, -1);
  }
  s = s.replace(/[$\s,]/g, "");
  if (s.startsWith("-")) {
    negative = !negative;
    s = s.slice(1);
  }
  if (!/^\d+(\.\d+)?$/.test(s)) return undefined;
  return negative ? `-${s}` : s;
}

// --- header lookup -----------------------------------------------------------

/** Find the index of the first header cell that contains any of `names`
 * (case-insensitive), or -1. */
function colIndex(header: readonly string[], ...names: string[]): number {
  const lower = header.map((h) => h.trim().toLowerCase());
  for (const name of names) {
    const target = name.toLowerCase();
    const idx = lower.findIndex((h) => h === target || h.includes(target));
    if (idx !== -1) return idx;
  }
  return -1;
}

/** Locate the header row (the first row that has the expected column names). */
function findHeader(rows: readonly string[][], ...required: string[]): number {
  for (let r = 0; r < rows.length; r++) {
    const row = rows[r] as string[];
    if (required.every((name) => colIndex(row, name) !== -1)) return r;
  }
  return -1;
}

const isTotalRow = (label: string): boolean => /^total\b/i.test(label.trim());

// --- account-type normalization ---------------------------------------------

/** Map QuickBooks' verbose account-type strings (as they appear in CSV exports)
 * onto the `QboAccountType` vocabulary. Unknown types fall back to Expense with
 * a best-effort keyword match. */
export function normalizeQboAccountType(raw: string): QboAccountType {
  const s = raw.trim().toLowerCase();
  const has = (...k: string[]) => k.some((x) => s.includes(x));
  if (has("bank")) return "Bank";
  if (has("accounts receivable", "a/r")) return "Accounts Receivable";
  if (has("accounts payable", "a/p")) return "Accounts Payable";
  if (has("credit card")) return "Credit Card";
  if (has("fixed asset")) return "Fixed Asset";
  if (has("other current asset")) return "Other Current Asset";
  if (has("other asset")) return "Other Asset";
  if (has("other current liabilit")) return "Other Current Liability";
  if (has("long term liabilit", "long-term liabilit")) return "Long Term Liability";
  if (has("equity")) return "Equity";
  if (has("cost of goods", "cogs")) return "Cost of Goods Sold";
  if (has("other income")) return "Other Income";
  if (has("income", "revenue", "sales")) return "Income";
  if (has("other expense")) return "Other Expense";
  if (has("expense")) return "Expense";
  // asset/liability catch-alls after the specific checks
  if (has("asset")) return "Other Current Asset";
  if (has("liabilit")) return "Other Current Liability";
  return "Expense";
}

// --- Chart of Accounts CSV ---------------------------------------------------

export function parseAccountsCsv(text: string): QboAccount[] {
  const rows = parseCsv(text);
  const h = findHeader(rows, "account", "type");
  if (h === -1) throw new Error("accounts CSV: could not find an 'Account'/'Type' header row");
  const header = rows[h] as string[];
  const nameCol = colIndex(header, "account name", "account", "name");
  const numCol = colIndex(header, "account number", "number", "num");
  const typeCol = colIndex(header, "type");
  const out: QboAccount[] = [];
  for (let r = h + 1; r < rows.length; r++) {
    const row = rows[r] as string[];
    const name = (row[nameCol] ?? "").trim();
    if (name === "" || isTotalRow(name)) continue;
    const typeRaw = (row[typeCol] ?? "").trim();
    if (typeRaw === "") continue;
    const acct: QboAccount = { name, type: normalizeQboAccountType(typeRaw) };
    const num = numCol !== -1 ? (row[numCol] ?? "").trim() : "";
    out.push(num !== "" ? { ...acct, acctNum: num } : acct);
  }
  return out;
}

// --- Trial Balance CSV -------------------------------------------------------

export function parseTrialBalanceCsv(text: string): QboTrialBalanceRow[] {
  const rows = parseCsv(text);
  const h = findHeader(rows, "debit", "credit");
  if (h === -1) throw new Error("trial balance CSV: could not find a 'Debit'/'Credit' header row");
  const header = rows[h] as string[];
  const debitCol = colIndex(header, "debit");
  const creditCol = colIndex(header, "credit");
  // account label is the first column that isn't debit/credit
  const nameCol = header.findIndex((_, idx) => idx !== debitCol && idx !== creditCol);
  const out: QboTrialBalanceRow[] = [];
  for (let r = h + 1; r < rows.length; r++) {
    const row = rows[r] as string[];
    const account = (row[nameCol] ?? "").trim();
    if (account === "" || isTotalRow(account)) continue;
    const debit = parseAmount(row[debitCol]);
    const credit = parseAmount(row[creditCol]);
    if (debit === undefined && credit === undefined) continue;
    out.push({
      account,
      ...(debit !== undefined ? { debit } : {}),
      ...(credit !== undefined ? { credit } : {}),
    });
  }
  return out;
}

// --- Journal report CSV ------------------------------------------------------

/**
 * Parse a QBO **Journal** report CSV into journal entries. In that report each
 * transaction is a block: the first line carries the Date (and usually a
 * Transaction Type + Num), following lines have a blank Date and continue the
 * same transaction. We start a new entry whenever a Date cell is non-empty.
 */
export function parseJournalCsv(text: string): QboJournalEntry[] {
  const rows = parseCsv(text);
  const h = findHeader(rows, "date", "account");
  if (h === -1) throw new Error("journal CSV: could not find a 'Date'/'Account' header row");
  const header = rows[h] as string[];
  const dateCol = colIndex(header, "date");
  const typeCol = colIndex(header, "transaction type", "type");
  const numCol = colIndex(header, "num", "number");
  const acctCol = colIndex(header, "account");
  const debitCol = colIndex(header, "debit");
  const creditCol = colIndex(header, "credit");
  const memoCol = colIndex(header, "memo", "description");

  const entries: QboJournalEntry[] = [];
  let current: { id: string; date: string; lines: QboJournalLine[]; memo?: string } | null = null;
  let counter = 0;

  const flush = () => {
    if (current && current.lines.length > 0) {
      entries.push({
        id: current.id,
        date: current.date,
        lines: current.lines,
        ...(current.memo !== undefined ? { memo: current.memo } : {}),
      });
    }
    current = null;
  };

  for (let r = h + 1; r < rows.length; r++) {
    const row = rows[r] as string[];
    const date = (row[dateCol] ?? "").trim();
    const account = (row[acctCol] ?? "").trim();
    if (isTotalRow(account) || (date !== "" && isTotalRow((row[typeCol] ?? "").trim()))) continue;

    if (date !== "") {
      flush();
      counter += 1;
      const type = typeCol !== -1 ? (row[typeCol] ?? "").trim() : "";
      const num = numCol !== -1 ? (row[numCol] ?? "").trim() : "";
      const id = [type.replace(/\s+/g, "-").toLowerCase() || "txn", num || String(counter)]
        .filter((x) => x !== "")
        .join("-");
      current = { id, date, lines: [] };
    }
    if (current === null || account === "") continue;

    const debit = parseAmount(row[debitCol]);
    const credit = parseAmount(row[creditCol]);
    if (debit === undefined && credit === undefined) continue;
    const memo = memoCol !== -1 ? (row[memoCol] ?? "").trim() : "";
    current.lines.push({
      account,
      ...(debit !== undefined ? { debit } : {}),
      ...(credit !== undefined ? { credit } : {}),
      ...(memo !== "" ? { memo } : {}),
    });
  }
  flush();
  return entries;
}

// --- combined ----------------------------------------------------------------

export interface QboCsvFiles {
  readonly accountsCsv: string;
  readonly journalCsv: string;
  readonly trialBalanceCsv?: string;
}

/** Assemble a full `QboExport` from the three QBO CSV reports. */
export function parseQboCsvExport(files: QboCsvFiles): QboExport {
  const accounts = parseAccountsCsv(files.accountsCsv);
  const entries = parseJournalCsv(files.journalCsv);
  const reportedTrialBalance =
    files.trialBalanceCsv !== undefined ? parseTrialBalanceCsv(files.trialBalanceCsv) : undefined;
  return {
    accounts,
    entries,
    ...(reportedTrialBalance !== undefined ? { reportedTrialBalance } : {}),
  };
}
