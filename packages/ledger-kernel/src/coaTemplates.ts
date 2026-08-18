import { ChartOfAccounts } from "./chartOfAccounts.js";
import { USD, type Currency } from "./money.js";
import { AccountSubtype, accountTypeOfSubtype, type Account, type AccountId } from "./types.js";

/**
 * Prebuilt charts of accounts by business category.
 *
 * A brand-new tenant shouldn't start from an empty ledger — they should start
 * from a sensible chart for *their kind of business*. This defines a shared base
 * chart every service business needs (bank, undeposited funds, AR/AP, sales-tax
 * payable, credit card, equity, retained earnings, standard income + operating
 * expenses) plus category-specific accounts layered on top (a contractor's job
 * materials and subcontractors, a restaurant's food/beverage COGS and tips
 * payable, a retailer's inventory and merchant fees, …).
 *
 * Each account's `type` is *derived* from its {@link AccountSubtype}, so a
 * template can never declare a mismatched type; `buildChartForCategory` returns
 * a fully validated {@link ChartOfAccounts}. Codes follow the standard numbering
 * (1xxx assets, 2xxx liabilities, 3xxx equity, 4xxx income, 5xxx COGS, 6xxx+
 * expenses), so the code-prefix fallbacks elsewhere still line up.
 */

export enum BusinessCategory {
  SERVICE_GENERAL = "SERVICE_GENERAL",
  PROFESSIONAL_SERVICES = "PROFESSIONAL_SERVICES",
  CONTRACTOR_TRADES = "CONTRACTOR_TRADES",
  RETAIL = "RETAIL",
  ECOMMERCE = "ECOMMERCE",
  RESTAURANT = "RESTAURANT",
  REAL_ESTATE = "REAL_ESTATE",
  HEALTHCARE_PRACTICE = "HEALTHCARE_PRACTICE",
  NONPROFIT = "NONPROFIT",
}

/** A template line: code + name + subtype. The account type is derived. */
interface TemplateLine {
  readonly code: string;
  readonly name: string;
  readonly subtype: AccountSubtype;
}

export interface CoaTemplateMeta {
  readonly category: BusinessCategory;
  readonly label: string;
  readonly description: string;
  readonly accountCount: number;
}

const S = AccountSubtype;

/** The base chart every service business starts from. */
const BASE: readonly TemplateLine[] = [
  // Assets (1xxx)
  { code: "1000", name: "Business Checking", subtype: S.BANK },
  { code: "1010", name: "Business Savings", subtype: S.BANK },
  { code: "1050", name: "Undeposited Funds", subtype: S.UNDEPOSITED_FUNDS },
  { code: "1200", name: "Accounts Receivable", subtype: S.ACCOUNTS_RECEIVABLE },
  { code: "1400", name: "Prepaid Expenses", subtype: S.OTHER_CURRENT_ASSET },
  { code: "1500", name: "Equipment", subtype: S.FIXED_ASSET },
  { code: "1510", name: "Accumulated Depreciation", subtype: S.FIXED_ASSET },
  // Liabilities (2xxx)
  { code: "2000", name: "Accounts Payable", subtype: S.ACCOUNTS_PAYABLE },
  { code: "2100", name: "Credit Card", subtype: S.CREDIT_CARD },
  { code: "2200", name: "Sales Tax Payable", subtype: S.SALES_TAX_PAYABLE },
  { code: "2300", name: "Payroll Liabilities", subtype: S.OTHER_CURRENT_LIABILITY },
  { code: "2700", name: "Long-Term Debt", subtype: S.LONG_TERM_LIABILITY },
  // Equity (3xxx)
  { code: "3000", name: "Owner's Equity", subtype: S.EQUITY },
  { code: "3100", name: "Owner's Draw", subtype: S.EQUITY },
  { code: "3900", name: "Retained Earnings", subtype: S.RETAINED_EARNINGS },
  // Income (4xxx)
  { code: "4000", name: "Services Income", subtype: S.INCOME },
  { code: "4900", name: "Other Income", subtype: S.OTHER_INCOME },
  // COGS (5xxx)
  { code: "5000", name: "Cost of Services", subtype: S.COST_OF_GOODS_SOLD },
  // Operating expenses (6xxx)
  { code: "6000", name: "Advertising & Marketing", subtype: S.EXPENSE },
  { code: "6050", name: "Bank & Merchant Fees", subtype: S.EXPENSE },
  { code: "6100", name: "Insurance", subtype: S.EXPENSE },
  { code: "6200", name: "Payroll — Wages", subtype: S.EXPENSE },
  { code: "6210", name: "Payroll Taxes", subtype: S.EXPENSE },
  { code: "6300", name: "Rent & Lease", subtype: S.EXPENSE },
  { code: "6400", name: "Office Supplies", subtype: S.EXPENSE },
  { code: "6500", name: "Software & Subscriptions", subtype: S.EXPENSE },
  { code: "6600", name: "Professional Fees", subtype: S.EXPENSE },
  { code: "6700", name: "Utilities", subtype: S.EXPENSE },
  { code: "6800", name: "Travel & Meals", subtype: S.EXPENSE },
  { code: "6900", name: "Depreciation Expense", subtype: S.OTHER_EXPENSE },
];


/**
 * Job-costing accounts, shared by the categories that run work as projects.
 *
 * A contractor's P&L is not the interesting question — "did *this job* make
 * money, and is it ahead of or behind its billing" is. That needs somewhere to
 * park cost that has been incurred but not yet billed, somewhere to park
 * billing that has run ahead of cost, and a retainage account for the 5–10%
 * the customer holds back until the job is signed off. Without those three, a
 * percent-complete schedule has nowhere to post and job profit is a guess.
 */
const JOB_COSTING: readonly TemplateLine[] = [
  { code: "1250", name: "Costs in Excess of Billings", subtype: S.OTHER_CURRENT_ASSET },
  { code: "1260", name: "Retainage Receivable", subtype: S.OTHER_CURRENT_ASSET },
  { code: "1270", name: "Work in Progress", subtype: S.OTHER_CURRENT_ASSET },
  { code: "2400", name: "Customer Deposits", subtype: S.OTHER_CURRENT_LIABILITY },
  { code: "2450", name: "Billings in Excess of Costs", subtype: S.OTHER_CURRENT_LIABILITY },
  { code: "5500", name: "Job Labor", subtype: S.COST_OF_GOODS_SOLD },
];

/** Category-specific accounts layered on top of the base. */
const EXTRAS: Readonly<Record<BusinessCategory, readonly TemplateLine[]>> = {
  [BusinessCategory.SERVICE_GENERAL]: [],
  [BusinessCategory.PROFESSIONAL_SERVICES]: [
    { code: "4100", name: "Consulting Income", subtype: S.INCOME },
    { code: "4200", name: "Retainer Income", subtype: S.INCOME },
    ...JOB_COSTING,
    // Same account as the contractor's 1250, under the name a consultancy uses
    // for it. Later lines win on a code collision, which is the point.
    { code: "1250", name: "Unbilled Receivables (WIP)", subtype: S.OTHER_CURRENT_ASSET },
    { code: "5100", name: "Subcontractor Costs", subtype: S.COST_OF_GOODS_SOLD },
    { code: "6610", name: "Continuing Education", subtype: S.EXPENSE },
  ],
  [BusinessCategory.CONTRACTOR_TRADES]: [
    ...JOB_COSTING,
    { code: "1300", name: "Materials Inventory", subtype: S.INVENTORY },
    { code: "4100", name: "Contract Income", subtype: S.INCOME },
    { code: "4200", name: "Change Orders", subtype: S.INCOME },
    { code: "1600", name: "Vehicles", subtype: S.FIXED_ASSET },
    { code: "5100", name: "Job Materials", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5200", name: "Subcontractors", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5300", name: "Equipment Rental", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5400", name: "Permits & Inspection Fees", subtype: S.COST_OF_GOODS_SOLD },
    { code: "6110", name: "Workers' Compensation", subtype: S.EXPENSE },
    { code: "6810", name: "Vehicle & Fuel", subtype: S.EXPENSE },
  ],
  [BusinessCategory.RETAIL]: [
    { code: "1300", name: "Inventory Asset", subtype: S.INVENTORY },
    { code: "4100", name: "Sales — Merchandise", subtype: S.INCOME },
    { code: "4300", name: "Sales Discounts", subtype: S.INCOME },
    { code: "5100", name: "Cost of Goods Sold — Merchandise", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5200", name: "Freight & Shipping In", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5300", name: "Inventory Shrinkage", subtype: S.COST_OF_GOODS_SOLD },
    { code: "6020", name: "Merchant Processing Fees", subtype: S.EXPENSE },
  ],
  [BusinessCategory.ECOMMERCE]: [
    { code: "1300", name: "Inventory Asset", subtype: S.INVENTORY },
    { code: "4100", name: "Online Sales", subtype: S.INCOME },
    { code: "4300", name: "Marketplace Sales", subtype: S.INCOME },
    { code: "4400", name: "Shipping Income", subtype: S.INCOME },
    { code: "5100", name: "Cost of Goods Sold", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5200", name: "Shipping & Fulfillment", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5300", name: "Marketplace & Payment Fees", subtype: S.COST_OF_GOODS_SOLD },
    { code: "6020", name: "Merchant Processing Fees", subtype: S.EXPENSE },
    { code: "6510", name: "E-commerce Platform Fees", subtype: S.EXPENSE },
  ],
  [BusinessCategory.RESTAURANT]: [
    { code: "1300", name: "Food & Beverage Inventory", subtype: S.INVENTORY },
    { code: "2400", name: "Tips Payable", subtype: S.OTHER_CURRENT_LIABILITY },
    { code: "4100", name: "Food Sales", subtype: S.INCOME },
    { code: "4200", name: "Beverage Sales", subtype: S.INCOME },
    { code: "4300", name: "Catering Income", subtype: S.INCOME },
    { code: "5100", name: "Food Costs", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5200", name: "Beverage Costs", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5300", name: "Kitchen Supplies", subtype: S.COST_OF_GOODS_SOLD },
    { code: "6220", name: "Kitchen & Wait Staff Wages", subtype: S.EXPENSE },
  ],
  [BusinessCategory.REAL_ESTATE]: [
    { code: "1350", name: "Properties Held", subtype: S.OTHER_ASSET },
    { code: "2500", name: "Security Deposits Held", subtype: S.OTHER_CURRENT_LIABILITY },
    { code: "4100", name: "Commission Income", subtype: S.INCOME },
    { code: "4200", name: "Rental Income", subtype: S.INCOME },
    { code: "4300", name: "Management Fees", subtype: S.INCOME },
    { code: "5100", name: "Agent Commissions Paid", subtype: S.COST_OF_GOODS_SOLD },
    { code: "6120", name: "Property Insurance", subtype: S.EXPENSE },
    { code: "6710", name: "Property Maintenance", subtype: S.EXPENSE },
  ],
  [BusinessCategory.HEALTHCARE_PRACTICE]: [
    { code: "1250", name: "Insurance Receivables", subtype: S.ACCOUNTS_RECEIVABLE },
    { code: "1300", name: "Medical Supplies Inventory", subtype: S.INVENTORY },
    { code: "4100", name: "Patient Service Revenue", subtype: S.INCOME },
    { code: "4200", name: "Insurance Reimbursements", subtype: S.INCOME },
    { code: "4350", name: "Contractual Adjustments", subtype: S.INCOME },
    { code: "5100", name: "Medical Supplies", subtype: S.COST_OF_GOODS_SOLD },
    { code: "5200", name: "Lab & Diagnostic Costs", subtype: S.COST_OF_GOODS_SOLD },
    { code: "6220", name: "Clinical Staff Wages", subtype: S.EXPENSE },
    { code: "6620", name: "Malpractice Insurance", subtype: S.EXPENSE },
  ],
  [BusinessCategory.NONPROFIT]: [
    { code: "3050", name: "Net Assets Without Donor Restrictions", subtype: S.EQUITY },
    { code: "3060", name: "Net Assets With Donor Restrictions", subtype: S.EQUITY },
    { code: "4100", name: "Contributions & Donations", subtype: S.INCOME },
    { code: "4200", name: "Grant Revenue", subtype: S.INCOME },
    { code: "4300", name: "Program Service Fees", subtype: S.INCOME },
    { code: "4400", name: "Fundraising Events", subtype: S.INCOME },
    { code: "6250", name: "Program Expenses", subtype: S.EXPENSE },
    { code: "6260", name: "Fundraising Expenses", subtype: S.EXPENSE },
    { code: "6270", name: "Management & General", subtype: S.EXPENSE },
  ],
};

const LABELS: Readonly<Record<BusinessCategory, { label: string; description: string }>> = {
  [BusinessCategory.SERVICE_GENERAL]: { label: "General Service Business", description: "A standard services chart — the sensible default." },
  [BusinessCategory.PROFESSIONAL_SERVICES]: { label: "Professional Services", description: "Consulting, agencies, legal, accounting — retainers, WIP, subcontractors." },
  [BusinessCategory.CONTRACTOR_TRADES]: { label: "Contractor & Trades", description: "Construction and trades — job materials, subcontractors, deposits, equipment." },
  [BusinessCategory.RETAIL]: { label: "Retail", description: "Brick-and-mortar retail — inventory, COGS, merchant fees, shrinkage." },
  [BusinessCategory.ECOMMERCE]: { label: "E-commerce", description: "Online sellers — marketplace/payment fees, shipping, fulfillment." },
  [BusinessCategory.RESTAURANT]: { label: "Restaurant & Food Service", description: "Food/beverage COGS, tips payable, catering." },
  [BusinessCategory.REAL_ESTATE]: { label: "Real Estate", description: "Brokerage/property mgmt — commissions, rental income, deposits held." },
  [BusinessCategory.HEALTHCARE_PRACTICE]: { label: "Healthcare Practice", description: "Clinics/practices — patient revenue, insurance receivables, contractual adjustments." },
  [BusinessCategory.NONPROFIT]: { label: "Nonprofit", description: "Donor-restricted net assets, contributions, grants, program vs G&A." },
};

function lineToAccount(line: TemplateLine, currency: Currency): Account {
  return {
    id: `acct:${line.code}` as AccountId,
    code: line.code,
    name: line.name,
    type: accountTypeOfSubtype(line.subtype),
    currency,
    subtype: line.subtype,
    active: true,
  };
}

/** The template lines for a category (base + extras), sorted by code. */
export function templateLines(category: BusinessCategory): readonly TemplateLine[] {
  const merged = new Map<string, TemplateLine>();
  for (const l of BASE) merged.set(l.code, l);
  for (const l of EXTRAS[category]) merged.set(l.code, l); // extras override base on code collision
  return [...merged.values()].sort((a, b) => a.code.localeCompare(b.code));
}

/** Build a validated ChartOfAccounts for a business category. */
export function buildChartForCategory(
  category: BusinessCategory,
  currency: Currency = USD,
): ChartOfAccounts {
  const accounts = templateLines(category).map((l) => lineToAccount(l, currency));
  return new ChartOfAccounts(accounts);
}

/** The accounts for a category as domain objects (e.g. to persist via PgAccountStore). */
export function templateAccounts(
  category: BusinessCategory,
  currency: Currency = USD,
): Account[] {
  return templateLines(category).map((l) => lineToAccount(l, currency));
}

/** Metadata for every template — drives the onboarding / admin picker. */
export function listCoaTemplates(): CoaTemplateMeta[] {
  return Object.values(BusinessCategory).map((category) => ({
    category,
    label: LABELS[category].label,
    description: LABELS[category].description,
    accountCount: templateLines(category).length,
  }));
}
