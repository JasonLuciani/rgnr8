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

/** The QBO entities we track for continuous sync. */
const CDC_ENTITIES = ["Purchase", "Deposit", "Payment"] as const;
type QboEntityName = (typeof CDC_ENTITIES)[number];

interface QboRef {
  value?: string;
  name?: string;
}
interface QboMetaData {
  CreateTime?: string;
  LastUpdatedTime?: string;
}
interface QboEntity {
  Id?: string;
  /** In a CDC response, tombstoned records carry `status: "Deleted"`. */
  status?: string;
  TxnDate?: string;
  TotalAmt?: number;
  AccountRef?: QboRef;
  DepositToAccountRef?: QboRef;
  MetaData?: QboMetaData;
  [k: string]: unknown;
}
/** One block per queried entity, keyed by entity name (Purchase/Deposit/…). */
type QboQueryResponse = {
  [K in QboEntityName]?: QboEntity[];
} & {
  startPosition?: number;
  maxResults?: number;
};
interface QboCdcContainer {
  QueryResponse?: QboQueryResponse[];
}
interface QboFaultError {
  code?: string;
  Message?: string;
}
interface QboCdcResponse {
  CDCResponse?: QboCdcContainer[];
  /** Server timestamp of this change-set — becomes the next `changedSince`. */
  time?: string;
  Fault?: { Error?: QboFaultError[]; type?: string };
}

/**
 * QuickBooks Online continuous-sync connector using change-data-capture. Each
 * page queries the CDC endpoint for everything that changed since the
 * connection's `cursor` (an opaque QBO server timestamp) across Purchase,
 * Deposit and Payment entities, maps every returned record into the same
 * `RawRecord` shape the PlaidConnector emits, and advances the cursor to the
 * server's reported `time` — exactly like Plaid advances its sync cursor.
 * Records flagged `status: "Deleted"` are counted as tombstones, not emitted.
 *
 * QBO auth lives on the connection the same way Plaid's does: `accessToken` is
 * the OAuth bearer token, `baseUrl` is the API host, and the company `realm_id`
 * sits in `secrets` alongside any other provider credentials.
 *
 * To go live: provide an HttpClient backed by fetch and a real access token +
 * realm_id in the connection — nothing else changes.
 */
export class QboSyncConnector implements Connector {
  readonly provider = "qbo";

  async syncPage(conn: Connection, http: HttpClient, fetchedAt: string): Promise<SyncPage> {
    const realmId = conn.secrets?.["realm_id"] ?? "";
    // QBO CDC requires a `changedSince` timestamp; on the first sync there is no
    // cursor yet, so seed from the connection's last sync (or this fetch time).
    const changedSince = conn.cursor ?? conn.lastSyncedAt ?? fetchedAt;
    const entities = CDC_ENTITIES.join(",");
    const res = await http.request({
      method: "GET",
      url:
        `${conn.baseUrl}/v3/company/${realmId}/cdc` +
        `?entities=${entities}&changedSince=${encodeURIComponent(changedSince)}`,
      headers: {
        Authorization: `Bearer ${conn.accessToken}`,
        Accept: "application/json",
      },
    });

    const json = res.json as QboCdcResponse;

    if (res.status !== 200) {
      const code = json?.Fault?.Error?.[0]?.code ?? "";
      const faultType = json?.Fault?.type ?? "";
      if (res.status === 429) throw new RateLimitError();
      if (res.status === 401) throw new AuthExpiredError("QBO access token expired");
      if (res.status === 403 || faultType === "AUTHENTICATION")
        throw new AuthRevokedError("QBO access revoked");
      throw new ProviderHttpError(res.status, `QBO error ${res.status} ${code}`);
    }

    let removed = 0;
    const rawRecords: RawRecord[] = [];
    for (const container of json.CDCResponse ?? []) {
      for (const block of container.QueryResponse ?? []) {
        for (const entityName of CDC_ENTITIES) {
          for (const entity of block[entityName] ?? []) {
            if (entity.status === "Deleted") {
              removed += 1;
              continue;
            }
            rawRecords.push(this.toRawRecord(entityName, entity, fetchedAt));
          }
        }
      }
    }

    // Advance the cursor to the server's change-set time. If none was reported
    // (e.g. an empty change-set), advance to `fetchedAt` so the next poll only
    // asks for changes after this one — mirroring Plaid's always-advance cursor.
    const nextCursor = json.time ?? fetchedAt;

    return { rawRecords, nextCursor, hasMore: false, removed };
  }

  private toRawRecord(entityName: QboEntityName, entity: QboEntity, fetchedAt: string): RawRecord {
    const accountId =
      entity.AccountRef?.value ??
      entity.DepositToAccountRef?.value ??
      entityName.toLowerCase();
    const id = entity.Id ?? "";
    return {
      provider: "qbo",
      accountId,
      // Ids are only unique per entity type in QBO, so namespace by entity.
      externalId: `${entityName}:${id}`,
      payload: { entity: entityName, ...entity },
      fetchedAt,
      sourceVersion: "qbo-cdc-1",
    };
  }
}
