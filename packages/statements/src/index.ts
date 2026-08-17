/**
 * @deprecated Legacy statement engine. `@rgnr8/financial-statements` is now the
 * canonical engine — it computes the same income statement / balance sheet from
 * a trial balance plus cash flow, retained earnings, GL detail, comparatives,
 * cash-basis and budgets. New code should import from `@rgnr8/financial-statements`.
 * This package is retained only because the qbo-migrate go-live parallel-close
 * comparison is gated on its exact output; it will be removed once that gate is
 * repointed at the canonical engine (a change to make with a live QBO close in
 * hand, not blind).
 */
export {
  computeIncomeStatement,
  computeBalanceSheet,
  type LineItem,
  type Section,
  type IncomeStatement,
  type BalanceSheet,
} from "./statements.js";
export { renderIncomeStatement, renderBalanceSheet } from "./render.js";
