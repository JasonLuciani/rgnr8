import { createHmac, timingSafeEqual } from "node:crypto";
import type { ConnectionStore } from "./store.js";
import type { ConnectionHealth } from "./types.js";

/**
 * Plaid delivers webhooks instead of requiring constant polling. This module
 * turns a raw webhook payload into a decision — sync now, flag for reconnect, or
 * ignore — and applies any health change to the connection. The caller owns the
 * side effect of actually running `ConnectorRunner.sync` when told to, so this
 * stays pure and testable (no HTTP here).
 */

export type WebhookAction =
  | "sync" // new data available — run an incremental sync
  | "reconnect" // auth is dead — the owner must re-link
  | "reconnect_soon" // auth will expire — nudge the owner
  | "ignore" // nothing actionable
  | "unknown_item" // no connection matched the item
  | "invalid_signature"; // the payload failed signature verification

export interface PlaidWebhook {
  readonly webhook_type?: string;
  readonly webhook_code?: string;
  readonly item_id?: string;
  readonly error?: { readonly error_code?: string } | null;
}

export interface WebhookOutcome {
  readonly action: WebhookAction;
  readonly connectionId?: string;
  readonly newHealth?: ConnectionHealth;
  readonly reason: string;
}

/** Resolve which connection a webhook is about. Generic over the payload shape
 * so each provider can supply its own resolver; defaults to Plaid's payload for
 * backward compatibility. Default Plaid behaviour: match a Plaid connection
 * whose `secrets.item_id` equals the payload's `item_id`. */
export type ConnectionResolver<P = PlaidWebhook> = (payload: P) => string | undefined;

export function defaultResolver(store: ConnectionStore): ConnectionResolver {
  return (payload) => {
    if (payload.item_id === undefined) return undefined;
    const match = store
      .list()
      .find((c) => c.provider === "plaid" && c.secrets?.["item_id"] === payload.item_id);
    return match?.id;
  };
}

export function handlePlaidWebhook(
  store: ConnectionStore,
  payload: PlaidWebhook,
  resolve?: ConnectionResolver,
): WebhookOutcome {
  const resolver = resolve ?? defaultResolver(store);
  const connectionId = resolver(payload);
  if (connectionId === undefined) {
    return { action: "unknown_item", reason: `no connection for item ${payload.item_id ?? "?"}` };
  }

  const type = payload.webhook_type ?? "";
  const code = payload.webhook_code ?? "";

  // Transactions updates → there is new data to pull.
  if (type === "TRANSACTIONS") {
    if (
      code === "SYNC_UPDATES_AVAILABLE" ||
      code === "DEFAULT_UPDATE" ||
      code === "INITIAL_UPDATE" ||
      code === "HISTORICAL_UPDATE"
    ) {
      return { action: "sync", connectionId, reason: `transactions ${code}` };
    }
    return { action: "ignore", connectionId, reason: `unhandled transactions code ${code}` };
  }

  // Item lifecycle → auth health.
  if (type === "ITEM") {
    if (code === "ERROR") {
      const errCode = payload.error?.error_code ?? "";
      const health = healthForItemError(errCode);
      applyHealth(store, connectionId, health, `ITEM ERROR ${errCode}`);
      return {
        action: "reconnect",
        connectionId,
        newHealth: health,
        reason: `item error ${errCode}`,
      };
    }
    if (code === "PENDING_EXPIRATION") {
      return { action: "reconnect_soon", connectionId, reason: "consent pending expiration" };
    }
    if (code === "USER_PERMISSION_REVOKED") {
      applyHealth(store, connectionId, "REVOKED", "USER_PERMISSION_REVOKED");
      return {
        action: "reconnect",
        connectionId,
        newHealth: "REVOKED",
        reason: "user revoked permission",
      };
    }
    return { action: "ignore", connectionId, reason: `unhandled item code ${code}` };
  }

  return { action: "ignore", connectionId, reason: `unhandled webhook_type ${type}` };
}

function healthForItemError(errorCode: string): ConnectionHealth {
  if (errorCode === "ITEM_LOGIN_REQUIRED") return "EXPIRED";
  if (errorCode === "INVALID_ACCESS_TOKEN" || errorCode === "ITEM_NOT_FOUND") return "REVOKED";
  return "ERROR";
}

function applyHealth(
  store: ConnectionStore,
  connectionId: string,
  health: ConnectionHealth,
  reason: string,
): void {
  const conn = store.get(connectionId);
  if (!conn) return;
  conn.health = health;
  conn.lastError = reason;
  store.put(conn);
}

// --- generic HMAC signature verification ------------------------------------

export interface HmacOptions {
  /** node:crypto hash name; defaults to "sha256". */
  readonly algorithm?: string;
  /** Digest/signature encoding; defaults to "hex". */
  readonly encoding?: "hex" | "base64";
  /** Optional header prefix to strip before comparing (e.g. "sha256="). */
  readonly prefix?: string;
}

/**
 * Verify an HMAC signature over `payload` using `secret`. Generic (algorithm,
 * encoding and an optional header prefix are configurable) so it fits any
 * provider that signs its webhooks with an HMAC. The comparison is
 * constant-time (`crypto.timingSafeEqual`) and never throws on a malformed or
 * wrong-length signature — it simply returns false — so it is safe to call on
 * untrusted input.
 */
export function verifyHmacSignature(
  payload: string,
  signature: string,
  secret: string,
  opts: HmacOptions = {},
): boolean {
  const algorithm = opts.algorithm ?? "sha256";
  const encoding = opts.encoding ?? "hex";
  const prefix = opts.prefix ?? "";

  let provided = signature;
  if (prefix.length > 0 && provided.startsWith(prefix)) {
    provided = provided.slice(prefix.length);
  }

  const expected = createHmac(algorithm, secret).update(payload, "utf8").digest(encoding);
  const providedBuf = Buffer.from(provided, encoding);
  const expectedBuf = Buffer.from(expected, encoding);
  // Unequal length can't match; bail before timingSafeEqual (which throws on
  // mismatched lengths). This branch leaks only the length, never the bytes.
  if (providedBuf.length !== expectedBuf.length) return false;
  return timingSafeEqual(providedBuf, expectedBuf);
}

// --- Gusto webhooks ----------------------------------------------------------

/** A Gusto webhook event payload (subset of fields we act on). */
export interface GustoWebhook {
  readonly event_type?: string;
  readonly entity_type?: string;
  readonly entity_uuid?: string;
  readonly resource_type?: string;
  readonly resource_uuid?: string;
  /** The Gusto company the event belongs to. */
  readonly company_uuid?: string;
}

/** An inbound Gusto webhook request: the raw body (as received, for signature
 * verification), the signature header value, and the shared signing secret. */
export interface GustoWebhookRequest {
  readonly rawBody: string;
  readonly signature: string;
  readonly secret: string;
  readonly hmac?: HmacOptions;
}

/** Default Gusto resolver: match a Gusto connection whose `secrets.company_id`
 * equals the event's company (or resource) uuid. */
export function gustoResolver(store: ConnectionStore): ConnectionResolver<GustoWebhook> {
  return (payload) => {
    const companyId = payload.company_uuid ?? payload.resource_uuid;
    if (companyId === undefined) return undefined;
    const match = store
      .list()
      .find((c) => c.provider === "gusto" && c.secrets?.["company_id"] === companyId);
    return match?.id;
  };
}

/**
 * Handle an inbound Gusto webhook, mirroring `handlePlaidWebhook`: verify the
 * HMAC signature first (rejecting anything unsigned or tampered), then resolve
 * the connection and turn the event into a `WebhookOutcome`. Stays pure — no
 * HTTP — so the caller owns running the actual sync when told to.
 */
export function handleGustoWebhook(
  store: ConnectionStore,
  req: GustoWebhookRequest,
  resolve?: ConnectionResolver<GustoWebhook>,
): WebhookOutcome {
  if (!verifyHmacSignature(req.rawBody, req.signature, req.secret, req.hmac)) {
    return { action: "invalid_signature", reason: "hmac signature verification failed" };
  }

  let payload: GustoWebhook;
  try {
    payload = JSON.parse(req.rawBody) as GustoWebhook;
  } catch {
    return { action: "ignore", reason: "unparseable webhook body" };
  }

  const resolver = resolve ?? gustoResolver(store);
  const connectionId = resolver(payload);
  if (connectionId === undefined) {
    const company = payload.company_uuid ?? payload.resource_uuid ?? "?";
    return { action: "unknown_item", reason: `no connection for company ${company}` };
  }

  const event = payload.event_type ?? "";
  // Processed / paid payroll runs mean there is new data to pull.
  if (event === "payroll.processed" || event === "payroll.paid" || event === "payroll.submitted") {
    return { action: "sync", connectionId, reason: `payroll event ${event}` };
  }

  return { action: "ignore", connectionId, reason: `unhandled event_type ${event}` };
}
