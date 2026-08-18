import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { LedgerService, ServiceRequest } from "./handlers.js";

/**
 * A thin node:http adapter over {@link LedgerService}. All the behavior lives in
 * the handlers (pure request → response); this only moves bytes: parse the URL
 * and body, call `handle`, write JSON back. Keeping it this thin is why the API
 * is fully testable without opening a socket.
 */

// A journal batch is tiny; an attached receipt is not. Base64 inflates a file
// by a third, so this is the 8 MB attachment limit plus encoding plus headroom.
const MAX_BODY_BYTES = 12_000_000;

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("request body too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

export function toServiceRequest(
  method: string,
  url: string,
  headers: Readonly<Record<string, string>>,
  body: string,
): ServiceRequest {
  const parsed = new URL(url, "http://ledger.local");
  const query: Record<string, string> = {};
  for (const [k, v] of parsed.searchParams) query[k] = v;
  // Path segments are percent-DECODED. Ids in this system legitimately contain
  // characters a client must escape — a journal entry is "acme:1" — and a
  // handler comparing an escaped path to an unescaped id fails to find a record
  // that is right there.
  const path = "/" + parsed.pathname
    .split("/")
    .filter((segment, i) => i > 0 || segment !== "")
    .map((segment) => {
      try {
        return decodeURIComponent(segment);
      } catch {
        return segment;   // a malformed escape is not a reason to 500
      }
    })
    .join("/");
  return { method: method.toUpperCase(), path, query, body, headers };
}

export function createLedgerServer(service: LedgerService): Server {
  return createServer((req: IncomingMessage, res: ServerResponse) => {
    void (async () => {
      try {
        const body = await readBody(req);
        const headers: Record<string, string> = {};
        for (const [k, v] of Object.entries(req.headers)) {
          if (typeof v === "string") headers[k.toLowerCase()] = v;
        }
        const result = await service.handle(
          toServiceRequest(req.method ?? "GET", req.url ?? "/", headers, body),
        );
        const payload = JSON.stringify(result.body);
        res.writeHead(result.status, {
          "content-type": "application/json; charset=utf-8",
          "content-length": Buffer.byteLength(payload),
        });
        res.end(payload);
      } catch (err) {
        const payload = JSON.stringify({
          error: err instanceof Error ? err.message : "internal error",
        });
        res.writeHead(400, { "content-type": "application/json; charset=utf-8" });
        res.end(payload);
      }
    })();
  });
}
