import type { HttpClient, HttpRequest, HttpResponse } from "./types.js";

/**
 * Test transport. Scripted with a list of responses (returned in order) or a
 * handler function. Records the requests it received for assertions.
 */
export class FakeHttpClient implements HttpClient {
  readonly requests: HttpRequest[] = [];
  private i = 0;

  constructor(
    private readonly script: readonly HttpResponse[] | ((req: HttpRequest, n: number) => HttpResponse),
  ) {}

  request(req: HttpRequest): Promise<HttpResponse> {
    this.requests.push(req);
    if (typeof this.script === "function") {
      return Promise.resolve(this.script(req, this.i++));
    }
    const res = this.script[this.i++];
    if (!res) throw new Error("FakeHttpClient: no scripted response left");
    return Promise.resolve(res);
  }
}
