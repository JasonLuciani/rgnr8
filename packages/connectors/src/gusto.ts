import type { RawRecord } from "@rgnr8/ingestion";
import {
  AuthExpiredError,
  ProviderHttpError,
  RateLimitError,
  type Connection,
  type Connector,
  type HttpClient,
  type SyncPage,
} from "./types.js";

interface GustoPayroll {
  payroll_uuid: string;
  check_date: string;
  totals?: { net_pay?: string; employer_taxes?: string; employee_taxes?: string };
  tax_settlement_date?: string;
}

/**
 * Gusto payroll connector. Fetches processed payroll runs and maps each to a
 * RawRecord whose payload matches the ingestion Gusto adapter's shape (net pay,
 * employer/employee taxes, settlement date). Single page for now.
 */
export class GustoConnector implements Connector {
  readonly provider = "gusto";

  async syncPage(conn: Connection, http: HttpClient, fetchedAt: string): Promise<SyncPage> {
    const companyId = conn.secrets?.["company_id"] ?? "";
    const accountId = conn.secrets?.["bank_account_id"] ?? "payroll";
    const res = await http.request({
      method: "GET",
      url: `${conn.baseUrl}/v1/companies/${companyId}/payrolls?processed=true`,
      headers: { Authorization: `Bearer ${conn.accessToken}` },
    });

    if (res.status === 429) throw new RateLimitError();
    if (res.status === 401) throw new AuthExpiredError("Gusto token expired");
    if (res.status !== 200) throw new ProviderHttpError(res.status, `Gusto error ${res.status}`);

    const runs = (res.json as GustoPayroll[]) ?? [];
    const rawRecords: RawRecord[] = runs.map((r) => ({
      provider: "gusto",
      accountId,
      externalId: r.payroll_uuid,
      payload: {
        payroll_id: r.payroll_uuid,
        account_id: accountId,
        pay_date: r.check_date,
        net_pay: r.totals?.net_pay ?? "0.00",
        employer_taxes: r.totals?.employer_taxes ?? "0.00",
        employee_taxes: r.totals?.employee_taxes ?? "0.00",
        tax_settlement_date: r.tax_settlement_date ?? r.check_date,
      },
      fetchedAt,
      sourceVersion: "gusto-sync-1",
    }));

    return { rawRecords, nextCursor: undefined, hasMore: false, removed: 0 };
  }
}
