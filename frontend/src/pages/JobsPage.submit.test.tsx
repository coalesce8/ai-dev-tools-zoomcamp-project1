import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import type { Stats } from "../api/stats";
import type { Job } from "../api/types";
import { jsonResponse } from "../test/http";
import JobsPage from "./JobsPage";

const T = Date.parse("2026-10-26T10:00:00Z");

function makeJob(over: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    name: "nightly",
    spec: { duration_s: 30, fail_probability: 0, mode: "normal" },
    state: "QUEUED",
    max_attempts: 3,
    attempt_count: 0,
    created_at: new Date(T).toISOString(),
    started_at: null,
    finished_at: null,
    last_error: null,
    ...over,
  };
}

const stats: Stats = {
  as_of: new Date(T).toISOString(),
  window_seconds: 600,
  counts: {
    QUEUED: 0,
    RUNNING: 0,
    SUCCEEDED: 0,
    FAILED: 0,
    STALLED: 0,
    CANCELLED: 0,
  },
  oldest_queued_age_seconds: null,
  jobs_finished: 0,
  jobs_failed: 0,
  job_failure_rate: null,
  attempts_finished: 0,
  attempts_failed_infra: 0,
  infra_attempt_failure_rate: null,
};

let fetchMock: ReturnType<typeof vi.fn>;
let items: Job[];
let postResponse: () => Response;

function getsOf(prefix: string): string[] {
  return fetchMock.mock.calls
    .filter(
      (c) =>
        (c[0] as string).split("?")[0] === prefix &&
        (c[1] as RequestInit | undefined)?.method !== "POST",
    )
    .map((c) => c[0] as string);
}

function renderPage(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <JobsPage />
    </MemoryRouter>,
  );
}

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function submit(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByRole("textbox", { name: "Name" }), "fresh");
  await user.click(screen.getByRole("button", { name: "Submit job" }));
  await flush();
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(T);
  items = [];
  postResponse = () => jsonResponse(201, makeJob({ id: "new", name: "fresh" }));
  fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      return Promise.resolve(postResponse());
    }
    if (url === "/api/v1/stats") {
      return Promise.resolve(jsonResponse(200, stats));
    }
    return Promise.resolve(jsonResponse(200, { items, next_cursor: null }));
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("JobsPage submit wiring", () => {
  it("puts the form under the heading and above the summary bar", async () => {
    renderPage();
    await flush();
    const heading = screen.getByRole("heading", { level: 1, name: "Jobs" });
    const form = screen.getByRole("form", { name: "Submit a job" });
    const summary = screen.getByRole("region", { name: "Summary" });
    expect(
      heading.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      form.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("refreshes jobs and stats at once after a successful submit", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPage();
    await flush();
    expect(getsOf("/api/v1/jobs")).toHaveLength(1);
    expect(getsOf("/api/v1/stats")).toHaveLength(1);
    items = [makeJob({ id: "new", name: "fresh" })];
    await submit(user);
    expect(getsOf("/api/v1/jobs")).toHaveLength(2);
    expect(getsOf("/api/v1/stats")).toHaveLength(2);
    expect(
      await screen.findByRole("link", { name: "fresh" }),
    ).toBeInTheDocument();
  });

  it("keeps the state filter in the refresh", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPage("/?state=FAILED");
    await flush();
    await submit(user);
    expect(getsOf("/api/v1/jobs")).toEqual([
      "/api/v1/jobs?state=FAILED",
      "/api/v1/jobs?state=FAILED",
    ]);
  });

  it("sends no extra GETs after a rejected submit", async () => {
    postResponse = () =>
      jsonResponse(400, {
        error: { code: "validation_error", message: "body.name: bad" },
      });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPage();
    await flush();
    await submit(user);
    expect(screen.getByText("body.name: bad")).toBeInTheDocument();
    expect(getsOf("/api/v1/jobs")).toHaveLength(1);
    expect(getsOf("/api/v1/stats")).toHaveLength(1);
  });
});
