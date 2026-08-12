import type { RawRecord } from "@rgnr8/ingestion";

// --- HTTP transport seam -----------------------------------------------------
export interface HttpRequest {
  readonly method: "GET" | "POST";
  readonly url: string;
  readonly headers?: Readonly<Record<string, string>>;
  readonly body?: unknown; // JSON-serializable
}

export interface HttpResponse {
  readonly status: number;
  readonly json: unknown;
}

/** The one thing to implement for production: wrap fetch. Tests use a fake. */
export interface HttpClient {
  request(req: HttpRequest): Promise<HttpResponse>;
}

// --- connection state --------------------------------------------------------
export type ConnectionHealth = "NEW" | "ACTIVE" | "EXPIRED" | "REVOKED" | "ERROR";

export interface Connection {
  readonly id: string;
  readonly provider: string;
  readonly tenantId: string;
  accessToken: string;
  /** OAuth refresh token, when the provider issues one (used by the refresher). */
  refreshToken?: string;
  /** ISO instant the access token expires, for pre-emptive/auto refresh. */
  accessTokenExpiresAt?: string;
  readonly baseUrl: string;
  /** Extra credentials (e.g. Plaid client_id/secret, Gusto company id). */
  readonly secrets?: Readonly<Record<string, string>>;
  /** Incremental sync cursor (provider-defined opaque string). */
  cursor?: string;
  health: ConnectionHealth;
  lastSyncedAt?: string;
  lastError?: string;
}

// --- connector contract ------------------------------------------------------
export interface SyncPage {
  readonly rawRecords: readonly RawRecord[];
  readonly nextCursor: string | undefined;
  readonly hasMore: boolean;
  readonly removed: number; // tombstoned records reported by the provider
}

export interface Connector {
  readonly provider: string;
  /** Fetch one page from the provider using the connection's cursor. */
  syncPage(conn: Connection, http: HttpClient, fetchedAt: string): Promise<SyncPage>;
}

// --- typed provider errors ---------------------------------------------------
export class AuthExpiredError extends Error {
  override readonly name = "AuthExpiredError";
}
export class AuthRevokedError extends Error {
  override readonly name = "AuthRevokedError";
}
export class RateLimitError extends Error {
  override readonly name = "RateLimitError";
  constructor(readonly retryAfterMs: number = 1000) {
    super("rate limited");
  }
}
export class ProviderHttpError extends Error {
  override readonly name = "ProviderHttpError";
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

// --- sync report -------------------------------------------------------------
export interface SyncIssue {
  readonly kind: string;
  readonly message: string;
}
export interface SyncReport {
  readonly connectionId: string;
  readonly rawRecords: readonly RawRecord[];
  readonly pages: number;
  readonly removed: number;
  readonly health: ConnectionHealth;
  readonly truncated: boolean;
  readonly issues: readonly SyncIssue[];
}
