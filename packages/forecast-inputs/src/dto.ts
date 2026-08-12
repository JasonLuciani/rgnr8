import type { Money } from "@rgnr8/ledger-kernel";

/**
 * DTOs mirroring the Python `rgnr8_forecast.io` contract (forecast-inputs/1).
 * Money is integer minor units; emitted as a JS number when safe, else a
 * numeric string, so no precision is lost across the language boundary.
 */
export const CONTRACT_VERSION = "forecast-inputs/1";

export interface MoneyDTO {
  minor: number | string;
  currency: string;
}

export type FrequencyDTO =
  | "WEEKLY"
  | "BIWEEKLY"
  | "SEMIMONTHLY"
  | "MONTHLY"
  | "QUARTERLY"
  | "ANNUAL";

export interface RecurrenceDTO {
  frequency: FrequencyDTO;
  anchor: string; // YYYY-MM-DD
  interval: number;
  second_day: number | null;
  end: string | null;
  count: number | null;
}

export interface RecurringItemDTO {
  label: string;
  category: string; // matches Python Category enum values
  direction: "INFLOW" | "OUTFLOW";
  amount: MoneyDTO;
  recurrence: RecurrenceDTO;
}

export interface OneTimeDTO {
  label: string;
  category: string;
  direction: "INFLOW" | "OUTFLOW";
  amount: MoneyDTO;
  on_date: string;
  confidence: string;
}

export interface OpeningDTO {
  as_of: string;
  available: MoneyDTO;
  restricted: MoneyDTO;
  verified: boolean;
}

export interface ForecastInputsDTO {
  contract: string;
  currency: string;
  opening: OpeningDTO;
  invoices: unknown[];
  customer_histories: unknown[];
  bills: unknown[];
  recurring: RecurringItemDTO[];
  payroll: unknown[];
  debt: unknown[];
  one_time: OneTimeDTO[];
  pipeline: unknown[];
}

export function moneyToDto(m: Money): MoneyDTO {
  const minor = m.minorUnits;
  const asNumber = Number(minor);
  const safe = BigInt(Number.isSafeInteger(asNumber) ? asNumber : NaN) === minor;
  return { minor: safe ? asNumber : minor.toString(), currency: m.currency.code };
}
