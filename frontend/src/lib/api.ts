/** Tiny typed fetch wrapper: cookie auth, CSRF, single-flight refresh (AUTH-5, SEC-2). */

export class ApiError extends Error {
  code: string;
  status: number;
  details: Record<string, unknown>;

  constructor(code: string, message: string, status: number, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

const RAW_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export const apiBase = `${RAW_BASE}/api/v1`;

export function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

export function csrfToken(): string | null {
  return readCookie("rf_csrf");
}

export type QueryValue = string | number | boolean | null | undefined | (string | number)[];

export function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = path.startsWith("http") ? path : `${apiBase}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      value.forEach((item) => params.append(key, String(item)));
    } else {
      params.append(key, String(value));
    }
  }
  const search = params.toString();
  return search ? `${url}?${search}` : url;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
  /** Skip the automatic refresh-and-retry (used by the auth calls themselves). */
  skipRefresh?: boolean;
  headers?: Record<string, string>;
}

function isMutating(method: string): boolean {
  return ["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase());
}

let refreshing: Promise<boolean> | null = null;

export function notifyUnauthorized(): void {
  window.dispatchEvent(new CustomEvent("rf:unauthorized"));
}

async function tryRefresh(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const response = await fetch(buildUrl("/auth/refresh"), {
          method: "POST",
          credentials: "include",
          headers: csrfToken() ? { "X-CSRF-Token": csrfToken() as string } : {},
        });
        return response.ok;
      } catch {
        return false;
      } finally {
        window.setTimeout(() => {
          refreshing = null;
        }, 0);
      }
    })();
  }
  return refreshing;
}

export interface RawResponse {
  ok: boolean;
  status: number;
  headers: Headers;
  json: () => Promise<unknown>;
  blob: () => Promise<Blob>;
  text: () => Promise<string>;
}

async function send(path: string, options: RequestOptions): Promise<RawResponse> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json", ...(options.headers ?? {}) };
  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (isMutating(method)) {
    const token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
  }
  const response = await fetch(buildUrl(path, options.query), {
    method,
    headers,
    body,
    credentials: "include",
    signal: options.signal,
  });
  return {
    ok: response.ok,
    status: response.status,
    headers: response.headers,
    json: () => response.json(),
    blob: () => response.blob(),
    text: () => response.text(),
  };
}

async function toError(response: Response | RawResponse): Promise<ApiError> {
  let code = "http_error";
  let message = `Request failed (${response.status})`;
  let details: Record<string, unknown> = {};
  try {
    const payload = (await response.json()) as { error?: { code?: string; message?: string; details?: Record<string, unknown> }; detail?: unknown };
    if (payload?.error) {
      code = payload.error.code ?? code;
      message = payload.error.message ?? message;
      details = payload.error.details ?? {};
    } else if (typeof payload?.detail === "string") {
      message = payload.detail;
    }
  } catch {
    /* no body */
  }
  return new ApiError(code, message, response.status, details);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response = await send(path, options);
  if (response.status === 401 && !options.skipRefresh && path !== "/auth/refresh" && path !== "/auth/login") {
    const refreshed = await tryRefresh();
    if (refreshed) {
      response = await send(path, options);
    } else {
      notifyUnauthorized();
      throw await toError(response);
    }
  }
  if (!response.ok) throw await toError(response);
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("json")) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function requestBlob(path: string, options: RequestOptions = {}): Promise<Blob> {
  let response = await send(path, options);
  if (response.status === 401 && !options.skipRefresh) {
    const refreshed = await tryRefresh();
    if (refreshed) response = await send(path, options);
    else notifyUnauthorized();
  }
  if (!response.ok) throw await toError(response);
  return response.blob();
}

const METHODS = {
  get: <T>(path: string, query?: Record<string, QueryValue>, signal?: AbortSignal) =>
    request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Record<string, QueryValue>) =>
    request<T>(path, { method: "POST", body, query }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body }),
  del: <T>(path: string, query?: Record<string, QueryValue>) =>
    request<T>(path, { method: "DELETE", query }),
  blob: (path: string, body?: unknown, query?: Record<string, QueryValue>) =>
    requestBlob(path, { method: body === undefined ? "GET" : "POST", body, query }),
};

export const api = { ...METHODS, request, requestBlob, buildUrl, apiBase };

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

export function isApiError(error: unknown, code?: string): error is ApiError {
  return error instanceof ApiError && (code === undefined || error.code === code);
}

/** Trigger a browser download without buffering the file in JS memory (RES-5, STO-1). */
export function triggerDownload(url: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

export async function downloadBlob(blob: Blob, filename?: string): Promise<void> {
  const url = URL.createObjectURL(blob);
  try {
    if (filename) {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.rel = "noopener";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } else {
      triggerDownload(url);
    }
    // Give the browser a moment to start the download before revoking.
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
  } finally {
    URL.revokeObjectURL(url);
  }
}
