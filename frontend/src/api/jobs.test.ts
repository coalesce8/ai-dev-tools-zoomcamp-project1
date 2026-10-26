import { jsonResponse } from "../test/http";
import { createJob, getJob, getJobLogs, listJobs } from "./jobs";
import type { Job, JobCreate, JobDetail, JobPage, LogPage } from "./types";

const job: Job = {
  id: "0b7c8a7e-0000-4000-8000-000000000001",
  name: "nightly",
  spec: { duration_s: 5, fail_probability: 0, mode: "normal" },
  state: "QUEUED",
  max_attempts: 3,
  attempt_count: 0,
  created_at: "2026-10-26T10:00:00+00:00",
  started_at: null,
  finished_at: null,
  last_error: null,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function lastCall(): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
  return { url, init };
}

describe("createJob", () => {
  it("POSTs the JSON body and resolves to the created job", async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, job));
    const body: JobCreate = { name: "nightly", spec: { duration_s: 5 }, max_attempts: 3 };
    await expect(createJob(body)).resolves.toEqual(job);
    const { url, init } = lastCall();
    expect(url).toBe("/api/v1/jobs");
    expect(init.method).toBe("POST");
    const headers = new Headers(init.headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("Accept")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual(body);
  });
});

describe("listJobs", () => {
  const page: JobPage = { items: [job], next_cursor: null };

  it("sends no query string without arguments", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page));
    await expect(listJobs()).resolves.toEqual(page);
    const { url, init } = lastCall();
    expect(url).toBe("/api/v1/jobs");
    expect(init.method ?? "GET").toBe("GET");
  });

  it("adds only the params that are given", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(200, page)));
    await listJobs({ state: "FAILED" });
    expect(lastCall().url).toBe("/api/v1/jobs?state=FAILED");
    await listJobs({ limit: 10 });
    expect(lastCall().url).toBe("/api/v1/jobs?limit=10");
  });

  it("URL-encodes the cursor so it arrives intact", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page));
    const cursor = "ab+c/d==";
    await listJobs({ state: "RUNNING", limit: 50, cursor });
    const url = new URL(lastCall().url, "http://localhost");
    expect(url.pathname).toBe("/api/v1/jobs");
    expect(url.searchParams.get("cursor")).toBe(cursor);
    expect(url.searchParams.get("state")).toBe("RUNNING");
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.search).not.toContain("+");
    expect(url.search).not.toContain("/");
  });
});

describe("getJob", () => {
  it("GETs the job by id", async () => {
    const detail: JobDetail = { ...job, attempts: [] };
    fetchMock.mockResolvedValue(jsonResponse(200, detail));
    await expect(getJob("abc")).resolves.toEqual(detail);
    expect(lastCall().url).toBe("/api/v1/jobs/abc");
  });

  it("URL-encodes the id", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ...job, attempts: [] }));
    await getJob("a/b");
    expect(lastCall().url).toBe("/api/v1/jobs/a%2Fb");
  });
});

describe("getJobLogs", () => {
  const logs: LogPage = {
    items: [{ seq: 4, attempt_number: 1, ts: "2026-10-26T10:00:01+00:00", line: "hello" }],
    next_after: 4,
    has_more: false,
  };

  it("sends after when given", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, logs));
    await expect(getJobLogs("abc", 3)).resolves.toEqual(logs);
    expect(lastCall().url).toBe("/api/v1/jobs/abc/logs?after=3");
  });

  it("sends after=0", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, logs));
    await getJobLogs("abc", 0);
    expect(lastCall().url).toBe("/api/v1/jobs/abc/logs?after=0");
  });

  it("omits after when left out", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, logs));
    await getJobLogs("abc");
    expect(lastCall().url).toBe("/api/v1/jobs/abc/logs");
  });

  it("URL-encodes the id", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, logs));
    await getJobLogs("a/b", 1);
    expect(lastCall().url).toBe("/api/v1/jobs/a%2Fb/logs?after=1");
  });
});
