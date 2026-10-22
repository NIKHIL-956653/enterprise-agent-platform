// One wrapper around fetch for the whole app. It attaches the bearer token, parses JSON,
// and turns a non-2xx into a typed error the UI can branch on - so no screen re-implements
// "did it fail, and how". The token lives in localStorage: readable by any script on the
// page, so an XSS could steal it. For a single-tenant admin console on localhost that is the
// right amount of engineering; the production answer is an httpOnly cookie plus CSRF, and it
// is worth saying so out loud in a review.

const BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";
const TOKEN_KEY = "eap.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode / blocked storage: the app still works for this session */
  }
}

export class ApiError extends Error {
  status: number;
  // FastAPI puts the useful part in `detail`: a string for our own HTTPExceptions, or a list
  // of field errors for a 422. Screens read this to show the real reason.
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

type Options = {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
};

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const headers: Record<string, string> = { ...(opts.headers ?? {}) };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch {
    // Network error, CORS block, server down - never a parsed response.
    throw new ApiError(0, null, "Could not reach the API. Is it running on " + BASE + "?");
  }

  let data: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail ?? data;
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`);
  }
  return data as T;
}
