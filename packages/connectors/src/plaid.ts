import type { RawRecord } from "@rgnr8/ingestion";
import {
  AuthExpiredError,
  AuthRevokedError,
  ProviderHttpError,
  RateLimitError,
  type Connection,
  type Connector,
  type HttpClient,
  type SyncPage,
} from "./types.js";

interface PlaidTxn {
  transaction_id: string;
  account_id: string;
}
interface PlaidSyncResponse {
  added?: PlaidTxn[];
  modified?: PlaidTxn[];
  removed?: { transaction_id: string }[];
  next_cursor?: string;
  has_more?: boolean;
  error_code?: string;
  error_type?: string;
}

/**
 * Plaid connector using the incremental `/transactions/sync` endpoint. The
 * connection's `cursor` is advanced each page; `added` and `modified`
 * transactions become RawRecords, `removed` are counted as tombstones.
 *
 * To go live: provide an HttpClient backed by fetch and real client_id/secret
 * + access_token in the connection — nothing else changes.
 */
export class PlaidConnector implements Connector {
  readonly provider = "plaid";

  async syncPage(conn: Connection, http: HttpClient, fetchedAt: string): Promise<SyncPage> {
    const res = await http.request({
      method: "POST",
      url: `${conn.baseUrl}/transactions/sync`,
      headers: { "Content-Type": "application/json" },
      body: {
        client_id: conn.secrets?.["client_id"],
        secret: conn.secrets?.["secret"],
        access_token: conn.accessToken,
        cursor: conn.cursor ?? "",
      },
    });

    const json = res.json as PlaidSyncResponse;

    if (res.status !== 200) {
      const code = json?.error_code ?? "";
      if (res.status === 429) throw new RateLimitError();
      if (code === "ITEM_LOGIN_REQUIRED") throw new AuthExpiredError("Plaid item login required");
      if (code === "INVALID_ACCESS_TOKEN" || code === "ITEM_NOT_FOUND")
        throw new AuthRevokedError("Plaid access token invalid/revoked");
      throw new ProviderHttpError(res.status, `Plaid error ${res.status} ${code}`);
    }

    const txns = [...(json.added ?? []), ...(json.modified ?? [])];
    const rawRecords: RawRecord[] = txns.map((t) => ({
      provider: "plaid",
      accountId: t.account_id,
      externalId: t.transaction_id,
      payload: t,
      fetchedAt,
      sourceVersion: "plaid-sync-1",
    }));

    return {
      rawRecords,
      nextCursor: json.next_cursor,
      hasMore: json.has_more ?? false,
      removed: (json.removed ?? []).length,
    };
  }
}
