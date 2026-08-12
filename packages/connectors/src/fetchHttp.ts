import type { HttpClient, HttpRequest, HttpResponse } from "./types.js";

/**
 * The minimal shape of `fetch` this client needs. Declared locally so the
 * package doesn't depend on the DOM lib, and so tests can inject a fake without
 * a network. Defaults to the runtime's global `fetch`.
 */
export type FetchLike = (
  url: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
  },
) => Promise<{ status: number; text(): Promise<string> }>;

export interface FetchHttpOptions {
  /** Injected for tests; defaults to globalThis.fetch. */
  readonly fetch?: FetchLike;
  /** Per-request timeout is the caller's concern; kept simple here. */
  readonly defaultHeaders?: Readonly<Record<string, string>>;
}

/**
 * The production `HttpClient`: wraps `fetch`, serializes JSON bodies, and parses
 * JSON responses defensively (a non-JSON or empty body becomes `{}` rather than
 * throwing, so the connector's own error mapping — not a parse crash — decides
 * the outcome). This is the single seam to swap for going live.
 */
export class FetchHttp implements HttpClient {
  private readonly fetchFn: FetchLike;
  private readonly defaultHeaders: Readonly<Record<string, string>>;

  constructor(opts: FetchHttpOptions = {}) {
    const globalFetch = (globalThis as { fetch?: FetchLike }).fetch;
    const chosen = opts.fetch ?? globalFetch;
    if (chosen === undefined) {
      throw new Error("no fetch available; pass opts.fetch");
    }
    this.fetchFn = chosen;
    this.defaultHeaders = opts.defaultHeaders ?? {};
  }

  async request(req: HttpRequest): Promise<HttpResponse> {
    const headers: Record<string, string> = {
      "content-type": "application/json",
      ...this.defaultHeaders,
      ...(req.headers ?? {}),
    };
    const init: { method?: string; headers?: Record<string, string>; body?: string } = {
      method: req.method,
      headers,
    };
    if (req.body !== undefined) init.body = JSON.stringify(req.body);

    const res = await this.fetchFn(req.url, init);
    let json: unknown = {};
    try {
      const text = await res.text();
      json = text ? (JSON.parse(text) as unknown) : {};
    } catch {
      json = {};
    }
    return { status: res.status, json };
  }
}
