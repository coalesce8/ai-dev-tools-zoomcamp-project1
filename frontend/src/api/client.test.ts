import { jsonResponse, textResponse } from "../test/http";
import { ApiError, apiFetch, errorMessage } from "./client";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function rejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (err) {
    return err;
  }
  throw new Error("expected the promise to reject");
}

async function expectApiError(
  promise: Promise<unknown>,
  expected: { status: number; code: string; message: string },
) {
  const err = await rejection(promise);
  expect(err).toBeInstanceOf(ApiError);
  const apiErr = err as ApiError;
  expect({ status: apiErr.status, code: apiErr.code, message: apiErr.message }).toEqual(expected);
}

describe("apiFetch request", () => {
  it("calls the relative /api/v1 URL with an Accept header", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }));
    await apiFetch("/jobs");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/jobs");
    expect(new Headers(init.headers).get("Accept")).toBe("application/json");
  });
});

describe("apiFetch success", () => {
  it("resolves to the parsed JSON body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { items: [1, 2] }));
    await expect(apiFetch("/x")).resolves.toEqual({ items: [1, 2] });
  });

  it("resolves to undefined for 204", async () => {
    fetchMock.mockResolvedValue(jsonResponse(204));
    await expect(apiFetch("/x")).resolves.toBeUndefined();
  });

  it("resolves to undefined for a 200 with an empty body", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 200 }));
    await expect(apiFetch("/x")).resolves.toBeUndefined();
  });

  it("rejects with invalid_response when a 2xx body is not JSON", async () => {
    fetchMock.mockResolvedValue(textResponse(200, "<html>hi</html>", "text/html"));
    const err = await rejection(apiFetch("/x"));
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("invalid_response");
    expect((err as ApiError).message).not.toBe("");
  });
});

describe("apiFetch error envelope", () => {
  it.each([
    [409, "conflict", "Job is already terminal"],
    [400, "validation_error", "name must not be empty"],
    [404, "not_found", "Job not found"],
  ])("maps status %i with code %s", async (status, code, message) => {
    fetchMock.mockResolvedValue(jsonResponse(status, { error: { code, message } }));
    await expectApiError(apiFetch("/jobs"), { status, code, message });
  });
});

describe("apiFetch errors without the envelope", () => {
  it.each([
    ["an HTML 502", () => textResponse(502, "<html>Bad Gateway</html>", "text/html"), 502],
    ["an empty 500", () => new Response(null, { status: 500 }), 500],
    ["JSON that is not the envelope", () => jsonResponse(422, { detail: "x" }), 422],
    ["an envelope with a non-string code", () => jsonResponse(400, { error: { code: 1, message: "m" } }), 400],
    ["an envelope with a non-string message", () => jsonResponse(400, { error: { code: "c", message: null } }), 400],
    ["an envelope whose error is not an object", () => jsonResponse(400, { error: "bad" }), 400],
    ["a JSON array", () => jsonResponse(500, [1]), 500],
  ])("maps %s to http_error", async (_label, makeResponse, status) => {
    fetchMock.mockResolvedValue(makeResponse());
    await expectApiError(apiFetch("/x"), {
      status,
      code: "http_error",
      message: `Request failed with status ${status}`,
    });
  });
});

describe("apiFetch network and abort", () => {
  it("maps a rejected fetch to network_error", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expectApiError(apiFetch("/x"), {
      status: 0,
      code: "network_error",
      message: "Could not reach the server",
    });
  });

  it("re-throws the original AbortError when the caller aborts", async () => {
    const abortError = new DOMException("The operation was aborted.", "AbortError");
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(abortError));
        }),
    );
    const controller = new AbortController();
    const promise = apiFetch("/x", { signal: controller.signal });
    controller.abort();
    const err = await rejection(promise);
    expect(err).toBe(abortError);
    expect(err).not.toBeInstanceOf(ApiError);
    expect(err).toBeInstanceOf(DOMException);
    expect((err as DOMException).name).toBe("AbortError");
  });

  it("passes the signal through to fetch", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    const controller = new AbortController();
    await apiFetch("/x", { signal: controller.signal });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.signal).toBe(controller.signal);
  });
});

describe("errorMessage", () => {
  it("returns the message of an ApiError", () => {
    expect(errorMessage(new ApiError(409, "conflict", "Job is already terminal"))).toBe(
      "Job is already terminal",
    );
  });

  it.each([
    ["a plain Error", new Error("boom")],
    ["a string", "boom"],
    ["undefined", undefined],
    ["null", null],
    ["an object", { message: "x" }],
    ["an ApiError with an empty message", new ApiError(500, "http_error", "")],
  ])("returns the fallback for %s", (_label, value) => {
    expect(errorMessage(value)).toBe("Something went wrong");
  });
});
