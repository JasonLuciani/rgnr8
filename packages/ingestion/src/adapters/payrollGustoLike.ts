import { Money, USD } from "@rgnr8/ledger-kernel";
import type { NormalizedInput, ProviderAdapter, RawRecord } from "../types.js";
import { TransactionKind } from "../types.js";

/** A Gusto-like payroll run. Amounts are positive decimal strings (money out). */
interface PayrollRun {
  payroll_id: string;
  account_id: string;
  pay_date: string;
  net_pay: string;
  employer_taxes?: string;
  employee_taxes?: string;
  tax_settlement_date?: string;
}

/**
 * Splits a payroll run into the cash movements it actually causes: net pay on
 * the pay date, and payroll taxes on the tax settlement date. This mirrors the
 * gross/net/liability timing the forecast and ledger both need.
 */
export class PayrollGustoLikeAdapter implements ProviderAdapter {
  readonly provider = "gusto";
  constructor(private readonly tenantId: string) {}

  normalize(raw: RawRecord): NormalizedInput[] {
    const p = raw.payload as PayrollRun;
    const out: NormalizedInput[] = [];

    const netOut = Money.fromDecimal(p.net_pay, USD).negate();
    out.push({
      tenantId: this.tenantId,
      accountId: p.account_id,
      externalId: `${p.payroll_id}:net`,
      status: "POSTED",
      date: p.pay_date,
      amount: netOut,
      description: "Payroll net pay",
      counterparty: "Payroll",
      kind: TransactionKind.PAYROLL_NET,
      source: this.ref(raw, p.payroll_id),
    });

    const taxesMinor =
      (p.employer_taxes ? Money.fromDecimal(p.employer_taxes, USD).minorUnits : 0n) +
      (p.employee_taxes ? Money.fromDecimal(p.employee_taxes, USD).minorUnits : 0n);
    if (taxesMinor > 0n) {
      out.push({
        tenantId: this.tenantId,
        accountId: p.account_id,
        externalId: `${p.payroll_id}:tax`,
        status: "POSTED",
        date: p.tax_settlement_date ?? p.pay_date,
        amount: Money.fromMinorUnits(-taxesMinor, USD),
        description: "Payroll taxes",
        counterparty: "Tax authority",
        kind: TransactionKind.PAYROLL_TAX,
        source: this.ref(raw, p.payroll_id),
      });
    }
    return out;
  }

  private ref(raw: RawRecord, payrollId: string) {
    return {
      provider: this.provider,
      sourceType: "payroll.run",
      sourceId: payrollId,
      sourceVersion: raw.sourceVersion,
      fetchedAt: raw.fetchedAt,
    };
  }
}
