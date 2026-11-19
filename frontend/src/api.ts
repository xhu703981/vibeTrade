// Local FastAPI (via the Vite /api proxy). Every state-changing request carries utterance: the user's raw words, attached separately by the frontend.
export type Json = Record<string, any>;

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

export type Api = {
  get: (path: string) => Promise<Json>;
  post: (path: string, body?: Json) => Promise<Json>;
};

/** A request that hangs must not hold one of the browser's few connections to the backend forever:
 * once they are all taken, every later request (including the agent's tool calls) waits behind them. */
export const REQUEST_TIMEOUT_MS = 20_000;

export function makeApi(base = "/api", f: typeof fetch = (...a) => fetch(...a), timeoutMs = REQUEST_TIMEOUT_MS): Api {
  async function call(method: string, path: string, body?: Json) {
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
    let r: Response;
    try {
      r = await f(base + path, {
        method,
        headers: body ? { "content-type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: ctl?.signal,
      });
    } catch (e) {
      if (ctl?.signal.aborted) throw new ApiError(0, "the local backend did not answer in time");
      throw e;
    } finally {
      if (timer) clearTimeout(timer);
    }
    let data: any = null;
    try {
      data = await r.json();
    } catch {
      data = null;
    }
    if (!r.ok) {
      const d = data?.detail;
      throw new ApiError(r.status, typeof d === "string" ? d : `HTTP ${r.status}`);
    }
    return data ?? {};
  }
  return { get: (p) => call("GET", p), post: (p, b) => call("POST", p, b) };
}
