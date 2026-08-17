import {
  Money,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  type AccountId,
  type JournalLineInput,
  type PostCommand,
} from "@rgnr8/ledger-kernel";

import type { DocPostContext } from "./documents.js";

/**
 * A full payroll journal — gross-to-net with per-employee detail and proper
 * liability accrual/clearing, replacing the old 2-line cash approximation.
 *
 * The standard entry a payroll run produces:
 *
 *   Dr  Wages Expense                (Σ gross)
 *   Dr  Employer Payroll Tax Expense (employer taxes)
 *     Cr  Net Pay / Cash               (Σ net)
 *     Cr  Employee Tax Withholdings Payable (Σ employee taxes)
 *     Cr  Employer Payroll Tax Payable      (employer taxes)
 *     Cr  Deductions Payable                (Σ deductions)
 *
 * It balances because per employee `net = gross - employee taxes - deductions`
 * (validated) and the employer tax expense equals the employer tax payable. The
 * liabilities sit on the balance sheet until the tax deposit / benefit remittance
 * clears them — which is what makes payroll a real accrual, not a cash shortcut.
 */

export interface EmployeePay {
  readonly employeeId: string;
  readonly grossPay: Money;
  /** Employee-side taxes withheld from gross. */
  readonly employeeTaxes: Money;
  /** Other pre/post-tax deductions withheld (benefits, 401k, garnishments). */
  readonly deductions?: Money;
  /** Net pay to the employee. Must equal gross − employeeTaxes − deductions. */
  readonly netPay: Money;
}

export interface PayrollRun {
  readonly id: string;
  readonly date: string; // ISO pay date
  readonly employees: readonly EmployeePay[];
  /** Employer-side payroll taxes (FICA match, FUTA/SUTA…) for the whole run. */
  readonly employerTaxes: Money;
}

export interface PayrollAccounts {
  readonly wagesExpense: AccountId;
  readonly employerTaxExpense: AccountId;
  /** Bank/cash (or a net-pay clearing account) the net pay is credited to. */
  readonly cash: AccountId;
  readonly employeeTaxPayable: AccountId;
  readonly employerTaxPayable: AccountId;
  readonly deductionsPayable: AccountId;
}

export class PayrollError extends Error {}

export interface PayrollTotals {
  readonly gross: Money;
  readonly employeeTaxes: Money;
  readonly deductions: Money;
  readonly net: Money;
  readonly employerTaxes: Money;
}

function zero(c: Money["currency"]): Money {
  return Money.zero(c);
}

/** Sum + validate a run's per-employee figures (net = gross − taxes − deductions). */
export function payrollTotals(run: PayrollRun, currency: Money["currency"]): PayrollTotals {
  if (run.employees.length === 0) throw new PayrollError(`Payroll run ${run.id} has no employees`);
  let gross = zero(currency);
  let employeeTaxes = zero(currency);
  let deductions = zero(currency);
  let net = zero(currency);
  for (const e of run.employees) {
    const ded = e.deductions ?? zero(currency);
    const expectedNet = e.grossPay.minus(e.employeeTaxes).minus(ded);
    if (!expectedNet.equals(e.netPay)) {
      throw new PayrollError(
        `Employee ${e.employeeId}: net ${e.netPay.toDecimalString()} != gross − taxes − deductions ${expectedNet.toDecimalString()}`,
      );
    }
    gross = gross.plus(e.grossPay);
    employeeTaxes = employeeTaxes.plus(e.employeeTaxes);
    deductions = deductions.plus(ded);
    net = net.plus(e.netPay);
  }
  return { gross, employeeTaxes, deductions, net, employerTaxes: run.employerTaxes };
}

/** Build the balanced payroll journal for a run. */
export function payrollRunToPostCommand(
  run: PayrollRun,
  accounts: PayrollAccounts,
  ctx: DocPostContext,
): PostCommand {
  const t = payrollTotals(run, ctx.currency);

  const lines: JournalLineInput[] = [
    { accountId: accounts.wagesExpense, side: "DEBIT", amount: t.gross, memo: "Gross wages" },
  ];
  if (!t.employerTaxes.isZero()) {
    lines.push({ accountId: accounts.employerTaxExpense, side: "DEBIT", amount: t.employerTaxes, memo: "Employer payroll taxes" });
  }
  lines.push({ accountId: accounts.cash, side: "CREDIT", amount: t.net, memo: "Net pay" });
  if (!t.employeeTaxes.isZero()) {
    lines.push({ accountId: accounts.employeeTaxPayable, side: "CREDIT", amount: t.employeeTaxes, memo: "Employee tax withholdings" });
  }
  if (!t.employerTaxes.isZero()) {
    lines.push({ accountId: accounts.employerTaxPayable, side: "CREDIT", amount: t.employerTaxes, memo: "Employer taxes payable" });
  }
  if (!t.deductions.isZero()) {
    lines.push({ accountId: accounts.deductionsPayable, side: "CREDIT", amount: t.deductions, memo: "Deductions payable" });
  }

  return {
    tenantId: asTenantId(ctx.tenantId),
    idempotencyKey: asIdempotencyKey(`payroll:${run.id}`),
    periodKey: asPeriodKey(run.date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: run.date,
    memo: `Payroll ${run.id}`,
    provenance: { ...ctx.provenance, mappingVersion: ctx.mappingVersion ?? ctx.provenance.mappingVersion },
    lines,
  };
}
