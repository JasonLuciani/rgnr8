import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { LedgerService, ServiceRequest } from "./handlers.js";

/**
 * A thin node:http adapter over {@link LedgerService}. All the behavior lives in
 * the handlers (pure request → response); this only moves bytes: parse the URL
 * and body, call `handle`, write JSON back. Keeping it this thin is why the API
 * is fully testable without opening a socket.
 */

const MAX_BODY_BYTES = 1_000_000; // a journal batch is small; cap to refuse abuse

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
  return { method: method.toUpperCase(), path: parsed.pathname, query, body, headers };
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
