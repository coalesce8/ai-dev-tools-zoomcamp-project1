// The browser always calls the API same-origin under /api/v1. In dev, Vite
// proxies it to API_PROXY_TARGET; in Compose and k8s the server in front of
// the built app does.
export const API_BASE = "/api/v1";

// Every failure of an API call, except an abort, becomes an ApiError.
// status is 0 when no HTTP response arrived.
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const FALLBACK_MESSAGE = "Something went wrong";

// The text a page shows for any error caught from an API call.
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError && err.message) {
    return err.message;
  }
  return FALLBACK_MESSAGE;
}

function isAbort(err: unknown, signal: AbortSignal | null | undefined): boolean {
  if (err instanceof DOMException && err.name === "AbortError") {
    return true;
  }
  return Boolean(signal?.aborted);
}

function parseEnvelope(text: string): { code: string; message: string } | null {
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof body !== "object" || body === null || !("error" in body)) {
    return null;
  }
  const error: unknown = body.error;
  if (typeof error !== "object" || error === null) {
    return null;
  }
  const { code, message } = error as { code?: unknown; message?: unknown };
  if (typeof code !== "string" || typeof message !== "string" || !code || !message) {
    return null;
  }
  return { code, message };
}

// Calls the API at /api/v1 + path. Resolves to the parsed JSON body, or to
// undefined for an empty 2xx body (such as 204).
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");

  let response: Response;
  let text: string;
  try {
    response = await fetch(API_BASE + path, { ...init, headers });
    text = await response.text();
  } catch (err) {
    if (isAbort(err, init.signal)) {
      throw err;
    }
    throw new ApiError(0, "network_error", "Could not reach the server");
  }

  if (!response.ok) {
    const envelope = parseEnvelope(text);
    if (envelope) {
      throw new ApiError(response.status, envelope.code, envelope.message);
    }
    throw new ApiError(
      response.status,
      "http_error",
      `Request failed with status ${response.status}`,
    );
  }

  if (text.trim() === "") {
    return undefined as T;
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, "invalid_response", "The server sent an invalid response");
  }
}
