// Builds a fetch Response for tests that stub `fetch`.
// A null or undefined body gives an empty body (use it for 204).
export function jsonResponse(status: number, body?: unknown): Response {
  const text = body === undefined || body === null ? null : JSON.stringify(body);
  return new Response(text, {
    status,
    headers: text === null ? {} : { "Content-Type": "application/json" },
  });
}

// Builds a Response with a raw text body, for non-JSON cases.
export function textResponse(status: number, body: string, contentType = "text/plain"): Response {
  return new Response(body, { status, headers: { "Content-Type": contentType } });
}
