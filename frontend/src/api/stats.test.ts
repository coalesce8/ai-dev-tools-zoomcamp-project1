import { ApiError } from "./client";
import { getStats } from "./stats";
import type { Stats } from "./stats";
import { jsonResponse } from "../test/http";

const stats: Stats = {
  as_of: "2026-10-26T10:00:00+00:00",
  window_seconds: 600,
  counts: { QUEUED: 1, RUNNING: 0, SUCCEEDED: 3, FAILED: 2, STALLED: 0, CANCELLED: 0 },
  oldest_queued_age_seconds: 12.5,
  jobs_finished: 5,
  jobs_failed: 2,
  job_failure_rate: 0.4,
  attempts_finished: 8,
  attempts_failed_infra: 0,
  infra_attempt_failure_rate: 0,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getStats", () => {
  it("GETs /api/v1/stats and resolves to the body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, stats));
    await expect(getStats()).resolves.toEqual(stats);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/stats");
    expect(init.method).toBeUndefined();
  });

  it("rejects with the server's code and message on a 500", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(500, { error: { code: "internal_error", message: "Database unavailable" } }),
    );
    const err = await getStats().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      status: 500,
      code: "internal_error",
      message: "Database unavailable",
    });
  });
});
