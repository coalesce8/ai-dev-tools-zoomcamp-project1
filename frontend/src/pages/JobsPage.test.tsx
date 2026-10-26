import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import type { Stats } from "../api/stats";
import { JOB_STATES } from "../api/types";
import type { Job, JobPage } from "../api/types";
import { jsonResponse, textResponse } from "../test/http";
import JobsPage, { REFRESH_INTERVAL_MS } from "./JobsPage";

const T = Date.parse("2026-10-26T10:00:00Z");

function makeJob(over: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    name: "nightly",
    spec: { duration_s: 30, fail_probability: 0, mode: "normal" },
    state: "QUEUED",
    max_attempts: 3,
    attempt_count: 0,
    created_at: new Date(T - 125_000).toISOString(),
    started_at: null,
    finished_at: null,
    last_error: null,
    ...over,
  };
}

function page(items: Job[], next_cursor: string | null = null): JobPage {
  return { items, next_cursor };
}

function makeStats(over: Partial<Stats> = {}): Stats {
  return {
    as_of: new Date(T).toISOString(),
    window_seconds: 600,
    counts: { QUEUED: 1, RUNNING: 0, SUCCEEDED: 0, FAILED: 0, STALLED: 0, CANCELLED: 0 },
    oldest_queued_age_seconds: null,
    jobs_finished: 0,
    jobs_failed: 0,
    job_failure_rate: null,
    attempts_finished: 0,
    attempts_failed_infra: 0,
    infra_attempt_failure_rate: null,
    ...over,
  };
}

const envelope = (message: string) => ({ error: { code: "internal_error", message } });

type Responder = (init: RequestInit) => Response | Promise<Response>;

let fetchMock: ReturnType<typeof vi.fn>;
let jobsQueue: Responder[];
let statsQueue: Responder[];
let jobsDefault: Responder;
let statsDefault: Responder;
let location: ReturnType<typeof useLocation>;

function LocationSpy() {
  location = useLocation();
  return null;
}

function urls(prefix: string): string[] {
  return fetchMock.mock.calls
    .map((c) => c[0] as string)
    .filter((u) => u === prefix || u.startsWith(prefix + "?"));
}

function inits(prefix: string): RequestInit[] {
  return fetchMock.mock.calls
    .filter((c) => (c[0] as string).split("?")[0] === prefix)
    .map((c) => c[1] as RequestInit);
}

function renderPage(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <JobsPage />
      <LocationSpy />
    </MemoryRouter>,
  );
}

async function tick(ms = REFRESH_INTERVAL_MS) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function flush() {
  await tick(0);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(T);
  jobsQueue = [];
  statsQueue = [];
  jobsDefault = () => jsonResponse(200, page([]));
  statsDefault = () => jsonResponse(200, makeStats());
  fetchMock = vi.fn((url: string, init: RequestInit) => {
    const isStats = url === "/api/v1/stats";
    const queue = isStats ? statsQueue : jobsQueue;
    const next = queue.shift() ?? (isStats ? statsDefault : jobsDefault);
    return Promise.resolve(next(init));
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("JobsPage table", () => {
  it("sends one jobs and one stats request on mount, with no query string", async () => {
    renderPage();
    await flush();
    expect(urls("/api/v1/jobs")).toEqual(["/api/v1/jobs"]);
    expect(urls("/api/v1/stats")).toEqual(["/api/v1/stats"]);
    expect(screen.getByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Summary" })).toBeInTheDocument();
  });

  it("shows Loading… before the first response", async () => {
    jobsQueue.push(() => new Promise<Response>(() => {}));
    statsQueue.push(() => new Promise<Response>(() => {}));
    renderPage();
    await flush();
    expect(screen.getAllByText("Loading…")).toHaveLength(2);
    expect(screen.queryByText("No jobs yet")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders headers and rows in API order", async () => {
    jobsQueue.push(() =>
      jsonResponse(
        200,
        page([
          makeJob({ id: "b", name: "second", state: "RUNNING", attempt_count: 2 }),
          makeJob({
            id: "a b",
            name: "first",
            spec: { duration_s: 600, fail_probability: 0, mode: "normal" },
          }),
        ]),
      ),
    );
    renderPage();
    await flush();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["State", "Name", "Attempts", "Age", "Duration"]);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    const [r1, r2] = rows.map((r) => within(r).getAllByRole("cell").map((c) => c.textContent));
    expect(r1).toEqual(["RUNNING", "second", "2/3", "2m 5s", "30s"]);
    expect(r2).toEqual(["QUEUED", "first", "0/3", "2m 5s", "10m"]);
    expect(screen.getByRole("link", { name: "second" })).toHaveAttribute("href", "/jobs/b");
    expect(screen.getByRole("link", { name: "first" })).toHaveAttribute("href", "/jobs/a%20b");
  });

  it("shows hostile and long names literally", async () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const long = "x".repeat(100);
    jobsQueue.push(() =>
      jsonResponse(
        200,
        page([makeJob({ id: "1", name: hostile }), makeJob({ id: "2", name: long })]),
      ),
    );
    const { container } = renderPage();
    await flush();
    expect(screen.getByText(hostile)).toBeInTheDocument();
    expect(screen.getByText(long)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
  });

  it("shows the empty text, with and without a filter", async () => {
    renderPage();
    await flush();
    expect(screen.getByText("No jobs yet")).toBeInTheDocument();
    cleanup();
    renderPage("/?state=FAILED");
    await flush();
    expect(screen.getByText("No FAILED jobs")).toBeInTheDocument();
  });

  it("says when there are more jobs than shown", async () => {
    const items = Array.from({ length: 50 }, (_, i) => makeJob({ id: `j${i}` }));
    jobsQueue.push(() => jsonResponse(200, page(items, "cursor")));
    renderPage();
    await flush();
    expect(screen.getByText("Showing the newest 50 jobs")).toBeInTheDocument();
  });

  it("omits that text when next_cursor is null", async () => {
    jobsQueue.push(() => jsonResponse(200, page([makeJob()])));
    renderPage();
    await flush();
    expect(screen.queryByText(/Showing the newest/)).not.toBeInTheDocument();
  });

  it("shows the age moving with each refresh", async () => {
    jobsDefault = () => jsonResponse(200, page([makeJob()]));
    renderPage();
    await flush();
    expect(screen.getByText("2m 5s")).toBeInTheDocument();
    await tick();
    expect(screen.getByText("2m 8s")).toBeInTheDocument();
  });
});

describe("JobsPage auto-refresh (AC-13)", () => {
  it("shows new jobs and state changes without user action", async () => {
    const a = makeJob({ id: "a", name: "job-a" });
    jobsQueue.push(() => jsonResponse(200, page([a])));
    jobsQueue.push(() =>
      jsonResponse(200, page([makeJob({ id: "b", name: "job-b" }), { ...a, state: "RUNNING" }])),
    );
    renderPage();
    await flush();
    expect(screen.getAllByRole("row")).toHaveLength(2);
    await tick();
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("job-b")).toBeInTheDocument();
    expect(within(rows[1]).getByText("RUNNING")).toBeInTheDocument();
  });

  it("refreshes stats on the same cycle", async () => {
    statsQueue.push(() => jsonResponse(200, makeStats()));
    statsQueue.push(() =>
      jsonResponse(
        200,
        makeStats({
          counts: { QUEUED: 9, RUNNING: 0, SUCCEEDED: 0, FAILED: 0, STALLED: 0, CANCELLED: 0 },
        }),
      ),
    );
    renderPage();
    await flush();
    const bar = screen.getByRole("region", { name: "Summary" });
    expect(within(bar).getAllByRole("listitem")[0]).toHaveTextContent("QUEUED 1");
    await tick();
    expect(within(bar).getAllByRole("listitem")[0]).toHaveTextContent("QUEUED 9");
  });

  it("never overlaps requests and waits the interval after a slow one settles", async () => {
    let release!: (r: Response) => void;
    jobsQueue.push(() => new Promise<Response>((res) => (release = res)));
    let releaseStats!: (r: Response) => void;
    statsQueue.push(() => new Promise<Response>((res) => (releaseStats = res)));
    renderPage();
    await flush();
    await tick(10_000);
    expect(urls("/api/v1/jobs")).toHaveLength(1);
    expect(urls("/api/v1/stats")).toHaveLength(1);
    await act(async () => {
      release(jsonResponse(200, page([])));
      releaseStats(jsonResponse(200, makeStats()));
    });
    await tick(REFRESH_INTERVAL_MS - 100);
    expect(urls("/api/v1/jobs")).toHaveLength(1);
    expect(urls("/api/v1/stats")).toHaveLength(1);
    await tick(100);
    expect(urls("/api/v1/jobs")).toHaveLength(2);
    expect(urls("/api/v1/stats")).toHaveLength(2);
  });

  it("stops and aborts in-flight requests on unmount, with no console errors", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    jobsQueue.push(() => jsonResponse(200, page([])));
    jobsQueue.push(() => new Promise<Response>(() => {}));
    const { unmount } = renderPage();
    await flush();
    await tick();
    const jobInits = inits("/api/v1/jobs");
    expect(jobInits).toHaveLength(2);
    expect(jobInits[1].signal?.aborted).toBe(false);
    const count = fetchMock.mock.calls.length;
    unmount();
    expect(jobInits[1].signal?.aborted).toBe(true);
    await tick(10_000);
    expect(fetchMock.mock.calls.length).toBe(count);
    expect(errorSpy).not.toHaveBeenCalled();
  });
});

describe("JobsPage state filter", () => {
  it("offers All and the six states, All selected by default", async () => {
    renderPage();
    await flush();
    const select = screen.getByRole("combobox", { name: "State" });
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["All", ...JOB_STATES]);
    expect(select).toHaveValue("");
    expect(screen.getByRole("option", { name: "All" })).toHaveProperty("selected", true);
  });

  it("fetches immediately on change, keeps the filter on refresh, and clears it for All", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderPage();
    await flush();
    const select = screen.getByRole("combobox", { name: "State" });
    await user.selectOptions(select, "FAILED");
    await flush();
    expect(urls("/api/v1/jobs").at(-1)).toBe("/api/v1/jobs?state=FAILED");
    expect(location.search).toBe("?state=FAILED");
    await tick();
    expect(urls("/api/v1/jobs").at(-1)).toBe("/api/v1/jobs?state=FAILED");
    await user.selectOptions(select, "All");
    await flush();
    expect(urls("/api/v1/jobs").at(-1)).toBe("/api/v1/jobs");
    expect(location.search).toBe("");
  });

  it("starts from the URL filter", async () => {
    renderPage("/?state=STALLED");
    await flush();
    expect(screen.getByRole("combobox", { name: "State" })).toHaveValue("STALLED");
    expect(urls("/api/v1/jobs")).toEqual(["/api/v1/jobs?state=STALLED"]);
  });

  it.each(["/?state=bogus", "/?state=failed", "/?state="])(
    "treats %s as All",
    async (path) => {
      renderPage(path);
      await flush();
      expect(screen.getByRole("combobox", { name: "State" })).toHaveValue("");
      expect(urls("/api/v1/jobs")).toEqual(["/api/v1/jobs"]);
    },
  );

  it("aborts the old request on a switch and never shows its late response", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    let release!: (r: Response) => void;
    jobsQueue.push(() => new Promise<Response>((res) => (release = res)));
    // Second call (filtered) never settles, so we can see the Loading… state.
    jobsQueue.push(() => new Promise<Response>(() => {}));
    renderPage();
    await flush();
    const firstInit = inits("/api/v1/jobs")[0];
    await user.selectOptions(screen.getByRole("combobox", { name: "State" }), "FAILED");
    await flush();
    expect(firstInit.signal?.aborted).toBe(true);
    await act(async () => {
      release(jsonResponse(200, page([makeJob({ name: "late-unfiltered" })])));
    });
    expect(screen.queryByText("late-unfiltered")).not.toBeInTheDocument();
    expect(screen.getAllByText("Loading…").length).toBeGreaterThan(0);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("clears the previous filter's rows while the new filter loads, and shows only API rows", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    jobsQueue.push(() => jsonResponse(200, page([makeJob({ name: "all-job" })])));
    jobsQueue.push(() => new Promise<Response>(() => {}));
    renderPage();
    await flush();
    expect(screen.getByText("all-job")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "State" }), "FAILED");
    await flush();
    expect(screen.queryByText("all-job")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("does not filter on the client", async () => {
    jobsQueue.push(() => jsonResponse(200, page([makeJob({ name: "queued-one", state: "QUEUED" })])));
    renderPage("/?state=FAILED");
    await flush();
    expect(screen.getByText("queued-one")).toBeInTheDocument();
  });

  it("calls stats the same way whatever the filter", async () => {
    renderPage("/?state=FAILED");
    await flush();
    expect(urls("/api/v1/stats")).toEqual(["/api/v1/stats"]);
  });
});

describe("JobsPage errors", () => {
  it("shows the server message, then recovers on the next poll", async () => {
    jobsQueue.push(() => jsonResponse(500, envelope("Database unavailable")));
    jobsQueue.push(() => jsonResponse(200, page([makeJob({ name: "back" })])));
    renderPage();
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("Database unavailable");
    expect(screen.queryByRole("row")).not.toBeInTheDocument();
    expect(screen.queryByText("No jobs yet")).not.toBeInTheDocument();
    await tick();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("back")).toBeInTheDocument();
  });

  it("keeps the last good rows when a later request fails, and clears on success", async () => {
    jobsQueue.push(() => jsonResponse(200, page([makeJob({ name: "kept" })])));
    jobsQueue.push(() => jsonResponse(500, envelope("Database unavailable")));
    jobsQueue.push(() => jsonResponse(200, page([makeJob({ name: "kept" })])));
    renderPage();
    await flush();
    await tick();
    expect(screen.getByRole("alert")).toHaveTextContent("Database unavailable");
    expect(screen.getByText("kept")).toBeInTheDocument();
    await tick();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it("shows a network failure message", async () => {
    jobsQueue.push(() => Promise.reject(new TypeError("Failed to fetch")) as never);
    renderPage();
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not reach the server");
  });

  it("shows a status message for an HTML 502", async () => {
    jobsQueue.push(() => textResponse(502, "<html>Bad Gateway</html>", "text/html"));
    renderPage();
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("Request failed with status 502");
  });

  it("keeps stats and jobs errors independent", async () => {
    statsQueue.push(() => jsonResponse(500, envelope("Stats broken")));
    renderPage();
    await flush();
    const bar = screen.getByRole("region", { name: "Summary" });
    expect(within(bar).getByRole("alert")).toHaveTextContent("Stats broken");
    expect(screen.getByText("No jobs yet")).toBeInTheDocument();
    cleanup();

    jobsQueue.push(() => jsonResponse(500, envelope("Jobs broken")));
    renderPage();
    await flush();
    const bar2 = screen.getByRole("region", { name: "Summary" });
    expect(within(bar2).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(bar2).getAllByRole("listitem")).toHaveLength(6);
    expect(screen.getByRole("alert")).toHaveTextContent("Jobs broken");
    cleanup();

    jobsQueue.push(() => jsonResponse(500, envelope("Jobs broken")));
    statsQueue.push(() => jsonResponse(500, envelope("Stats broken")));
    renderPage();
    await flush();
    expect(screen.getAllByRole("alert")).toHaveLength(2);
  });

  it("never shows [object Object], undefined or an empty alert", async () => {
    jobsQueue.push(() => jsonResponse(500, { weird: true }));
    statsQueue.push(() => jsonResponse(500, { error: {} }));
    const { container } = renderPage();
    await flush();
    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(2);
    for (const alert of alerts) {
      expect(alert.textContent).toMatch(/\S/);
    }
    expect(container.textContent).not.toContain("[object Object]");
    expect(container.textContent).not.toContain("undefined");
  });
});
