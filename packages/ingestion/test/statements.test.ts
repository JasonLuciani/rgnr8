import { test } from "node:test";
import assert from "node:assert/strict";
import {
  IngestionPipeline,
  StatementAdapter,
  TransactionKind,
  parseOfx,
  buildOfx,
  parseStatement,
  parseStatementCsv,
  statementRawRecords,
} from "../src/index.js";

const OFX = `OFXHEADER:100
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260815120000<TRNAMT>13210.55<FITID>T1<NAME>Client deposit</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260816<TRNAMT>-4000.00<FITID>T2<NAME>Rent<MEMO>September</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260817<TRNAMT>-35.00<FITID>T3<NAME>Monthly bank fee</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>`;

test("parseOfx extracts signed transactions with dates and ids", () => {
  const txns = parseOfx(OFX);
  assert.equal(txns.length, 3);
  assert.deepEqual(txns[0], { fitid: "T1", date: "2026-08-15", amount: "13210.55", description: "Client deposit", currency: "USD" });
  assert.equal(txns[1]?.amount, "-4000.00");
  assert.equal(txns[1]?.description, "Rent — September");
});

test("parseStatement sniffs OFX vs CSV", () => {
  assert.equal(parseStatement(OFX).length, 3);
  const csv = "Date,Description,Amount\n2026-08-15,Client deposit,13210.55\n08/16/2026,Rent,-4000.00\n";
  const rows = parseStatement(csv);
  assert.equal(rows.length, 2);
  assert.equal(rows[1]?.date, "2026-08-16"); // MM/DD/YYYY normalized
  assert.equal(rows[1]?.amount, "-4000.00");
});

test("CSV with separate debit/credit columns signs correctly", () => {
  const csv = "Posted Date,Memo,Debit,Credit\n2026-08-15,Deposit,,13210.55\n2026-08-16,Rent,4000.00,\n";
  const rows = parseStatementCsv(csv);
  assert.equal(rows[0]?.amount, "13210.55"); // credit → positive
  assert.equal(rows[1]?.amount, "-4000.00"); // debit → negative
});

test("statement records flow through the ingestion pipeline to canonical txns", () => {
  const raws = statementRawRecords(parseOfx(OFX), { accountId: "gl.checking", fetchedAt: "2026-09-01T00:00:00Z" });
  const pipe = new IngestionPipeline().register(new StatementAdapter("acme"));
  const result = pipe.ingest(raws);
  assert.equal(result.added.length, 3);
  const txns = pipe.active();
  const deposit = txns.find((t) => t.externalId === "T1");
  assert.equal(deposit?.direction, "INFLOW");
  assert.equal(deposit?.kind, TransactionKind.DEPOSIT);
  const fee = txns.find((t) => t.externalId === "T3");
  assert.equal(fee?.kind, TransactionKind.FEE);
  assert.equal(fee?.direction, "OUTFLOW");
});

test("re-importing the same statement is idempotent (dedupe by FITID)", () => {
  const raws = statementRawRecords(parseOfx(OFX), { accountId: "gl.checking", fetchedAt: "2026-09-01T00:00:00Z" });
  const pipe = new IngestionPipeline().register(new StatementAdapter("acme"));
  pipe.ingest(raws);
  const second = pipe.ingest(raws);
  assert.equal(second.added.length, 0);
  assert.equal(second.duplicates, 3);
  assert.equal(pipe.active().length, 3);
});

test("buildOfx round-trips through parseOfx (fitid/date/amount/description)", () => {
  const txns = [
    { fitid: "T1", date: "2026-08-15", amount: "13210.55", description: "Client deposit", currency: "USD" },
    { fitid: "T2", date: "2026-08-16", amount: "-4000.00", description: "Rent", currency: "USD" },
    { fitid: "T3", date: "2026-08-17", amount: "-35.00", description: "Monthly bank fee", currency: "USD" },
  ];
  const ofx = buildOfx(txns, { bankId: "021000021", acctId: "12345678", currency: "USD", dtServer: "20260817120000" });
  // Valid-looking OFX 1.02 SGML header + envelope.
  assert.match(ofx, /OFXHEADER:100/);
  assert.match(ofx, /<CURDEF>USD/);
  assert.match(ofx, /<TRNTYPE>DEBIT<DTPOSTED>20260816<TRNAMT>-4000\.00/);

  const back = parseOfx(ofx);
  assert.equal(back.length, 3);
  assert.deepEqual(back.map((t) => t.fitid), ["T1", "T2", "T3"]);
  assert.deepEqual(back.map((t) => t.date), ["2026-08-15", "2026-08-16", "2026-08-17"]);
  assert.deepEqual(back.map((t) => t.amount), ["13210.55", "-4000.00", "-35.00"]);
  assert.equal(back[0]!.description, "Client deposit");
  assert.equal(back[0]!.currency, "USD");
});

test("buildOfx classifies sign into CREDIT/DEBIT and is deterministic", () => {
  const a = buildOfx([{ fitid: "X", date: "2026-01-02", amount: "10.00", description: "in" }], { dtServer: "20260102120000" });
  const b = buildOfx([{ fitid: "X", date: "2026-01-02", amount: "10.00", description: "in" }], { dtServer: "20260102120000" });
  assert.equal(a, b);                                   // deterministic (injected dtServer)
  assert.match(a, /<TRNTYPE>CREDIT<DTPOSTED>20260102<TRNAMT>10\.00<FITID>X/);
});
