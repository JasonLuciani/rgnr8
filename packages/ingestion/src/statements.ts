import { Money, USD, defineCurrency } from "@rgnr8/ledger-kernel";
import type { NormalizedInput, ProviderAdapter, RawRecord } from "./types.js";
import { TransactionKind } from "./types.js";

/**
 * Bank **statement import** — the offline path when a live connector isn't
 * available (or as a belt-and-suspenders reconciliation source). Parses the two
 * formats every bank can export — **OFX/QFX** (the SGML the "download
 * transactions" button produces) and **CSV** — into a common `StatementTxn`,
 * then into the same `RawRecord`s the connectors emit, so they flow through the
 * exact same ingestion → reconciliation pipeline. No new downstream code path.
 *
 * Sign convention matches RGNR8's: a **positive** amount is money INTO the
 * account (a credit/deposit), negative is money out — which is already how OFX
 * `TRNAMT` and typical bank CSV amounts are signed, so no flipping.
 */

export interface StatementTxn {
  /** Provider's stable id (OFX FITID, or a synthesized hash for CSV). */
  readonly fitid: string;
  /** Effective/posted date, ISO (YYYY-MM-DD). */
  readonly date: string;
  /** Signed decimal string; positive = into the account. */
  readonly amount: string;
  readonly description: string;
  readonly currency?: string;
}

// --- OFX / QFX ---------------------------------------------------------------

function ofxTag(block: string, tag: string): string | undefined {
  // SGML: <TAG>value  (value runs to the next tag or end of line); tolerant of
  // both closed (<TAG>v</TAG>) and unclosed forms.
  const m = new RegExp(`<${tag}>([^<\r\n]*)`, "i").exec(block);
  return m ? m[1]?.trim() : undefined;
}

function ofxDate(raw: string | undefined): string | undefined {
  if (!raw || raw.length < 8) return undefined;
  const d = raw.slice(0, 8);
  if (!/^\d{8}$/.test(d)) return undefined;
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
}

/** Parse the STMTTRN records out of an OFX/QFX document. */
export function parseOfx(text: string): StatementTxn[] {
  const curMatch = /<CURDEF>([A-Z]{3})/i.exec(text);
  const currency = curMatch?.[1];
  const out: StatementTxn[] = [];
  const blocks = text.split(/<STMTTRN>/i).slice(1);
  for (const b of blocks) {
    const block = b.split(/<\/STMTTRN>/i)[0] ?? b;
    const date = ofxDate(ofxTag(block, "DTPOSTED"));
    const amount = ofxTag(block, "TRNAMT");
    if (date === undefined || amount === undefined) continue;
    const fitid = ofxTag(block, "FITID") ?? `${date}|${amount}`;
    const name = ofxTag(block, "NAME") ?? "";
    const memo = ofxTag(block, "MEMO") ?? "";
    const description = [name, memo].filter((s) => s !== "").join(" — ") || "(no description)";
    out.push({
      fitid,
      date,
      amount: amount.replace(/,/g, ""),
      description,
      ...(currency !== undefined ? { currency } : {}),
    });
  }
  return out;
}

// --- CSV ---------------------------------------------------------------------

function splitCsvLine(line: string): string[] {
  const cells: string[] = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          cur += '"';
          i++;
        } else inQuotes = false;
      } else cur += ch;
    } else if (ch === '"') inQuotes = true;
    else if (ch === ",") {
      cells.push(cur);
      cur = "";
    } else cur += ch;
  }
  cells.push(cur);
  return cells.map((c) => c.trim());
}

function normalizeCsvDate(raw: string): string | undefined {
  const s = raw.trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  const m = /^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/.exec(s); // MM/DD/YYYY
  if (m) return `${m[3]}-${m[1]!.padStart(2, "0")}-${m[2]!.padStart(2, "0")}`;
  return undefined;
}

function csvAmount(row: Record<string, string>): string | undefined {
  if (row["amount"] !== undefined && row["amount"] !== "") return row["amount"];
  // debit/credit column pair: credit positive, debit negative
  const debit = row["debit"] ?? "";
  const credit = row["credit"] ?? "";
  if (credit !== "") return credit;
  if (debit !== "") return debit.startsWith("-") ? debit : `-${debit}`;
  return undefined;
}

function djb2(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(16);
}

/**
 * Parse a bank CSV export. Header-driven and tolerant of the common column
 * names: date / posted date; description / memo / payee; amount (signed) OR a
 * debit+credit pair. Amounts keep the account sign convention (positive = in).
 */
export function parseStatementCsv(text: string): StatementTxn[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "");
  if (lines.length < 2) return [];
  const header = splitCsvLine(lines[0]!).map((h) => h.toLowerCase());
  const idx = (names: string[]): number => header.findIndex((h) => names.some((n) => h.includes(n)));
  const dateCol = idx(["date"]);
  const descCol = idx(["description", "memo", "payee", "name"]);
  const amtCol = idx(["amount"]);
  const debitCol = idx(["debit", "withdrawal"]);
  const creditCol = idx(["credit", "deposit"]);

  const out: StatementTxn[] = [];
  for (let i = 1; i < lines.length; i++) {
    const cells = splitCsvLine(lines[i]!);
    const date = dateCol >= 0 ? normalizeCsvDate(cells[dateCol] ?? "") : undefined;
    if (date === undefined) continue;
    const row: Record<string, string> = {
      amount: amtCol >= 0 ? (cells[amtCol] ?? "") : "",
      debit: debitCol >= 0 ? (cells[debitCol] ?? "") : "",
      credit: creditCol >= 0 ? (cells[creditCol] ?? "") : "",
    };
    const amtRaw = csvAmount(row);
    if (amtRaw === undefined) continue;
    const amount = amtRaw.replace(/[$\s,]/g, "");
    if (!/^-?\d+(\.\d+)?$/.test(amount)) continue;
    const description = (descCol >= 0 ? cells[descCol] : undefined) || "(no description)";
    out.push({ fitid: `csv-${djb2(`${date}|${amount}|${description}|${i}`)}`, date, amount, description });
  }
  return out;
}

/** Sniff the format and parse. OFX if it looks like OFX, else CSV. */
export function parseStatement(text: string): StatementTxn[] {
  return /<(OFX|STMTTRN)\b/i.test(text) || /OFXHEADER/i.test(text)
    ? parseOfx(text)
    : parseStatementCsv(text);
}

// --- into the ingestion pipeline ---------------------------------------------

export const STATEMENT_PROVIDER = "statement";

export interface StatementRawOptions {
  readonly accountId: string;
  readonly fetchedAt: string; // ISO; supplied by the caller, not generated
  readonly sourceVersion?: string;
}

/** Wrap parsed statement txns as `RawRecord`s for `IngestionPipeline.ingest`. */
export function statementRawRecords(
  txns: readonly StatementTxn[],
  opts: StatementRawOptions,
): RawRecord[] {
  return txns.map((t) => ({
    provider: STATEMENT_PROVIDER,
    accountId: opts.accountId,
    externalId: t.fitid,
    payload: t,
    fetchedAt: opts.fetchedAt,
    sourceVersion: opts.sourceVersion ?? "statement/1",
  }));
}

function currencyFor(code: string | undefined): ReturnType<typeof defineCurrency> {
  if (!code || code === "USD") return USD;
  return defineCurrency(code, 2);
}

function classify(inflow: boolean, description: string): TransactionKind {
  const n = description.toLowerCase();
  if (n.includes("transfer")) return TransactionKind.TRANSFER;
  if (inflow) {
    if (n.includes("refund")) return TransactionKind.REFUND;
    if (n.includes("interest")) return TransactionKind.INTEREST;
    return TransactionKind.DEPOSIT;
  }
  if (n.includes("fee")) return TransactionKind.FEE;
  return TransactionKind.PURCHASE;
}

/** The pipeline adapter for statement-imported records. Statement lines are
 * always POSTED (a statement is history), so there's no pending/supersession. */
export class StatementAdapter implements ProviderAdapter {
  readonly provider = STATEMENT_PROVIDER;
  constructor(private readonly tenantId: string) {}

  normalize(raw: RawRecord): NormalizedInput[] {
    const t = raw.payload as StatementTxn;
    const amount = Money.fromDecimal(t.amount, currencyFor(t.currency));
    const inflow = amount.minorUnits >= 0n;
    return [
      {
        tenantId: this.tenantId,
        accountId: raw.accountId,
        externalId: t.fitid,
        status: "POSTED",
        date: t.date,
        amount,
        description: t.description,
        kind: classify(inflow, t.description),
        source: {
          provider: STATEMENT_PROVIDER,
          sourceType: "bank.statement",
          sourceId: t.fitid,
          sourceVersion: raw.sourceVersion,
          fetchedAt: raw.fetchedAt,
        },
      },
    ];
  }
}
