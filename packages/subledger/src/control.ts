import type { Money } from "@rgnr8/ledger-kernel";

export interface ControlReconciliation {
  readonly name: string;
  readonly subledgerControl: Money;
  readonly glControl: Money;
  readonly difference: Money; // subledger − GL
  readonly balanced: boolean;
}

/**
 * Tie a subledger control total to its GL control account balance. The two must
 * agree; a nonzero difference means the subledger and the ledger have drifted
 * and the account is not clean.
 */
export function reconcileControl(name: string, subledgerControl: Money, glControl: Money): ControlReconciliation {
  const difference = subledgerControl.minus(glControl);
  return { name, subledgerControl, glControl, difference, balanced: difference.isZero() };
}
