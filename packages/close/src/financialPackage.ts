import { createHash } from "node:crypto";

/**
 * The immutable financial package for a closed period.
 *
 * When a period closes, its numbers must become a permanent, tamper-evident
 * artifact: the trial balance, the statements, and (when a QuickBooks parallel
 * close was run) the reconciliation to QBO — all frozen and fingerprinted. The
 * fingerprint is a SHA-256 over a canonical serialization of the *financial
 * content only* (not the envelope metadata), so it is reproducible from the
 * ledger: re-deriving the same books yields the same fingerprint, and any later
 * edit to a stored package is detectable with `verifyFinancialPackage`.
 *
 * Money is carried as minor-unit integer strings (e.g. "4600000") so the package
 * is exact and language-neutral — no floats, and no dependency on the kernel's
 * Money type here (this stays a leaf module).
 */

export const FINANCIAL_PACKAGE_VERSION = "financial-package/1";
export const FINGERPRINT_ALGORITHM = "sha256";

export interface PackagedTrialBalanceRow {
  readonly code: string;
  readonly name: string;
  readonly debitMinor: string;
  readonly creditMinor: string;
}

export interface PackagedTrialBalance {
  readonly rows: readonly PackagedTrialBalanceRow[];
  readonly totalDebitMinor: string;
  readonly totalCreditMinor: string;
  readonly inBalance: boolean;
}

export interface PackagedIncomeStatement {
  readonly revenueMinor: string;
  readonly expensesMinor: string;
  readonly netIncomeMinor: string;
}

export interface PackagedBalanceSheet {
  readonly totalAssetsMinor: string;
  readonly totalLiabilitiesAndEquityMinor: string;
  readonly netIncomeMinor: string;
  readonly balances: boolean;
}

export interface PackagedQboReconciliation {
  readonly inAgreement: boolean;
  readonly totalAbsDeltaMinor: string;
  readonly mismatchCount: number;
  readonly onlyInRgnr8Count: number;
  readonly onlyInQboCount: number;
}

/** The fingerprinted financial content — reproducible from the ledger. */
export interface FinancialPackageContent {
  readonly version: string;
  readonly periodKey: string;
  readonly currency: string;
  readonly trialBalance: PackagedTrialBalance;
  readonly incomeStatement: PackagedIncomeStatement;
  readonly balanceSheet: PackagedBalanceSheet;
  readonly qboReconciliation?: PackagedQboReconciliation;
}

/** The content plus a signing envelope (metadata that is NOT fingerprinted). */
export interface FinancialPackage extends FinancialPackageContent {
  readonly closedBy: string;
  readonly closedAt: string;
  readonly packagedAt: string;
  readonly algorithm: string;
  readonly fingerprint: string;
}

export interface PackageMeta {
  readonly closedBy: string;
  readonly closedAt: string;
  readonly packagedAt: string;
}

export type FinancialPackageInput = Omit<FinancialPackageContent, "version">;

/** Deterministic JSON: object keys sorted recursively so the hash is stable. */
function canonicalize(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalize(obj[k])}`).join(",")}}`;
}

export function fingerprintContent(content: FinancialPackageContent): string {
  return createHash("sha256").update(canonicalize(content), "utf8").digest("hex");
}

/**
 * Freeze a closed period's numbers into an immutable, fingerprinted package.
 * `version` is fixed and the fingerprint covers only the financial content, so
 * the same books always produce the same fingerprint regardless of who/when it
 * was packaged.
 */
export function buildFinancialPackage(
  input: FinancialPackageInput,
  meta: PackageMeta,
): FinancialPackage {
  const content: FinancialPackageContent = { version: FINANCIAL_PACKAGE_VERSION, ...input };
  const fingerprint = fingerprintContent(content);
  return {
    ...content,
    closedBy: meta.closedBy,
    closedAt: meta.closedAt,
    packagedAt: meta.packagedAt,
    algorithm: FINGERPRINT_ALGORITHM,
    fingerprint,
  };
}

export interface VerificationResult {
  readonly valid: boolean;
  readonly expected: string;
  readonly actual: string;
}

/**
 * Recompute the fingerprint from a package's financial content and compare it to
 * the stored one. A mismatch means the stored numbers were altered after sealing.
 */
export function verifyFinancialPackage(pkg: FinancialPackage): VerificationResult {
  const content: FinancialPackageContent = {
    version: pkg.version,
    periodKey: pkg.periodKey,
    currency: pkg.currency,
    trialBalance: pkg.trialBalance,
    incomeStatement: pkg.incomeStatement,
    balanceSheet: pkg.balanceSheet,
    ...(pkg.qboReconciliation !== undefined ? { qboReconciliation: pkg.qboReconciliation } : {}),
  };
  const actual = fingerprintContent(content);
  return { valid: actual === pkg.fingerprint, expected: pkg.fingerprint, actual };
}
