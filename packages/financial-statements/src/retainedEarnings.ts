/**
 * Statement of Retained Earnings — the roll-forward of accumulated earnings:
 *
 *   ending RE = beginning RE + net income − distributions
 *
 * All figures are credit-natural (a positive number is a credit balance, the
 * normal side for equity). Net income increases RE; a net loss (negative net
 * income) decreases it; owner distributions/dividends decrease it. This is the
 * bridge between the income statement and the equity section of the balance
 * sheet, and the thing the year-end close actually posts.
 */

import { Money } from "@rgnr8/ledger-kernel";
import type { Currency } from "@rgnr8/ledger-kernel";

export interface RetainedEarnings {
  readonly currency: Currency;
  readonly beginning: Money;
  readonly netIncome: Money;
  /** Owner draws / dividends declared in the period (credit-natural, subtracted). */
  readonly distributions: Money;
  readonly ending: Money;
}

export interface RetainedEarningsInput {
  readonly beginning: Money;
  readonly netIncome: Money;
  readonly distributions?: Money;
}

/** Build the retained-earnings roll-forward. */
export function retainedEarnings(input: RetainedEarningsInput): RetainedEarnings {
  const currency = input.beginning.currency;
  const distributions = input.distributions ?? Money.zero(currency);
  const ending = input.beginning.plus(input.netIncome).minus(distributions);
  return {
    currency,
    beginning: input.beginning,
    netIncome: input.netIncome,
    distributions,
    ending,
  };
}
