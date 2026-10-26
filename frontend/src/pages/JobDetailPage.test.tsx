import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router";
import { JOB_STATES } from "../api/types";
import type { Attempt, JobDetail, JobState, LogLine, LogPage } from "../api/types";
import { jsonResponse, textResponse } from "../test/http";
import JobDetailPage, { LOG_POLL_INTERVAL_MS } from "./JobDetailPage";

const T = "2026-10-26T10:00:00Z";

function makeAttempt(over: Partial<Attempt> = {}): Attempt {
  return {
    id: "att-1",
    number: 1,
    worker_id: "w1",
    state: "RUNNING",
    lease_expires_at: null,
    started_at: T,
    finished_at: null,
    exit_reason: null,
    failure_cause: null,
    ...over,
  };
}

function makeJob(over: Partial<JobDetail> = {}): JobDetail {
  return {
    id: "j1",
    name: "nightly",
    spec: { duration_s: 90, fail_probability: 0.25, mode: "normal" },
    state: "RUNNING",
    max_attempts: 3,
    attempt_count: 1,
    created_at: T,
    started_at: null,
    finished_at: null,
    last_error: null,
    attempts: [],
    ...over,
  };
}

function lines(from: number, to: number, over: Partial<LogLine> = {}): LogLine[] {
  const out: LogLine[] = [];
  for (let seq = from; seq <= to; seq++) {
    out.push({ seq, attempt_number: 1, ts: T, line: `line ${seq}`, ...over });
  }
  return out;
}

function logPage(items: LogLine[], next_after?: number, has_more = false): LogPage {
  return { items, next_after: next_after ?? items.at(-1)?.seq ?? 0, has_more };
}

const envelope = (code: string, message: string) => ({
  error: { code, message },
});

type Responder = (init: RequestInit) => Response | Promise<Response>;

let fetchMock: ReturnType<typeof vi.fn>;
let jobQueue: Responder[];
let logQueue: Responder[];
let postQueue: Responder[];
let jobDefault: Responder;
let logDefault: Responder;

function calls(match: (url: string, init: RequestInit) => boolean): string[] {
  return fetchMock.mock.calls
    .filter((c) => match(c[0] as string, (c[1] ?? {}) as RequestInit))
    .map((c) => c[0] as string);
}
const getJobs = () => calls((u, i) => !u.includes("/logs") && i.method !== "POST");
const getLogs = () => calls((u) => u.includes("/logs"));
const posts = () => calls((_u, i) => i.method === "POST");

function Nav() {
  const navigate = useNavigate();
  return <button onClick={() => void navigate("/jobs/b")}>go b</button>;
}

function renderPage(path = "/jobs/j1") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Nav />
      <Routes>
        <Route path="/jobs/:id" element={<JobDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}
const flush = () => advance(0);

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  jobQueue = [];
  logQueue = [];
  postQueue = [];
  jobDefault = () => jsonResponse(200, makeJob());
  logDefault = () => jsonResponse(200, logPage([]));
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    let queue: Responder[];
    let def: Responder;
    if (init.method === "POST") {
      queue = postQueue;
      def = () => jsonResponse(200, makeJob());
    } else if (url.includes("/logs")) {
      queue = logQueue;
      def = logDefault;
    } else {
      queue = jobQueue;
      def = jobDefault;
    }
    return Promise.resolve((queue.shift() ?? def)(init));
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("loading", () => {
  it("sends one job and one logs request, shows Loading… first", async () => {
    jobQueue.push(() => new Promise<Response>(() => {}));
    renderPage("/jobs/a%20b");
    await flush();
    expect(screen.getByRole("heading", { level: 1, name: "Job" })).toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Requeue|Cancel/ })).not.toBeInTheDocument();
    expect(getJobs()).toEqual(["/api/v1/jobs/a%20b"]);
  });

  it("requests logs with no after on mount", async () => {
    renderPage();
    await flush();
    expect(getJobs()).toEqual(["/api/v1/jobs/j1"]);
    expect(getLogs()).toEqual(["/api/v1/jobs/j1/logs"]);
  });

  it("shows a 404 in an alert, no sections, and stops polling", async () => {
    jobQueue.push(() => jsonResponse(404, envelope("not_found", "Job not found")));
    renderPage();
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("Job not found");
    expect(screen.queryByRole("button", { name: /Requeue|Cancel/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Attempts" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Logs" })).not.toBeInTheDocument();
    const before = fetchMock.mock.calls.length;
    await advance(10_000);
    expect(fetchMock.mock.calls.length).toBe(before);
  });
});

describe("header", () => {
  it("shows the job fields and em dashes for nulls", async () => {
    jobDefault = () =>
      jsonResponse(
        200,
        makeJob({
          state: "FAILED",
          attempt_count: 2,
          max_attempts: 3,
          last_error: "boom",
        }),
      );
    renderPage();
    await flush();
    expect(screen.getByText("nightly")).toBeInTheDocument();
    expect(screen.getByText("FAILED", { selector: ".state-badge" })).toBeInTheDocument();
    expect(screen.getByText("2/3")).toBeInTheDocument();
    expect(screen.getByText("1m 30s")).toBeInTheDocument();
    expect(screen.getByText("0.25")).toBeInTheDocument();
    expect(screen.getByText("normal")).toBeInTheDocument();
    expect(screen.getByText(T)).toBeInTheDocument();
    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.getByText("Last error")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("omits Last error when null", async () => {
    renderPage();
    await flush();
    expect(screen.queryByText("Last error")).not.toBeInTheDocument();
  });

  it("renders API strings as text", async () => {
    const evil = "<img src=x onerror=alert(1)>";
    jobDefault = () =>
      jsonResponse(
        200,
        makeJob({
          name: evil,
          attempts: [
            makeAttempt({
              state: "FAILED",
              exit_reason: evil,
              failure_cause: "workload",
            }),
          ],
        }),
      );
    logDefault = () => jsonResponse(200, logPage([{ ...lines(1, 1)[0], line: evil }]));
    renderPage();
    await flush();
    expect(screen.getAllByText(evil, { exact: false }).length).toBeGreaterThanOrEqual(3);
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("attempts", () => {
  it("lists every attempt in order with cause and run time", async () => {
    const attempts = [1, 2, 3, 4, 5].map((n) =>
      makeAttempt({
        id: `a${n}`,
        number: n,
        state: n === 5 ? "RUNNING" : "FAILED",
        failure_cause: n === 5 ? null : n === 4 ? "infra" : "workload",
        exit_reason: n === 5 ? null : `reason ${n}`,
        worker_id: n === 2 ? null : "w1",
        finished_at: n === 5 ? null : "2026-10-26T10:01:05Z",
      }),
    );
    jobDefault = () => jsonResponse(200, makeJob({ attempts }));
    renderPage();
    await flush();
    const section = screen.getByRole("region", { name: "Attempts" });
    const headings = within(section)
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(headings).toEqual(["Attempt 1", "Attempt 2", "Attempt 3", "Attempt 4", "Attempt 5"]);
    expect(within(section).getAllByText("workload")).toHaveLength(3);
    expect(within(section).getAllByText("infra")).toHaveLength(1);
    expect(within(section).getByText("reason 4")).toBeInTheDocument();
    expect(within(section).getAllByText("1m 5s")).toHaveLength(4);
    const last = within(section).getAllByRole("listitem")[4];
    expect(within(last).queryByText("Failure cause")).not.toBeInTheDocument();
    expect(within(last).getAllByText("—")).toHaveLength(2);
  });

  it("shows a cancelled attempt's reason and no cause", async () => {
    jobDefault = () =>
      jsonResponse(
        200,
        makeJob({
          state: "CANCELLED",
          attempts: [
            makeAttempt({
              state: "CANCELLED",
              exit_reason: "cancelled by user",
            }),
          ],
        }),
      );
    renderPage();
    await flush();
    expect(screen.getByText("cancelled by user")).toBeInTheDocument();
    expect(screen.queryByText("Failure cause")).not.toBeInTheDocument();
  });

  it("shows No attempts yet", async () => {
    jobDefault = () => jsonResponse(200, makeJob({ state: "QUEUED" }));
    renderPage();
    await flush();
    expect(screen.getByText("No attempts yet")).toBeInTheDocument();
  });

  it("gains the new attempt when a queued job starts", async () => {
    jobQueue.push(() => jsonResponse(200, makeJob({ state: "QUEUED" })));
    jobDefault = () => jsonResponse(200, makeJob({ attempts: [makeAttempt()] }));
    renderPage();
    await flush();
    expect(screen.getByText("No attempts yet")).toBeInTheDocument();
    await advance(LOG_POLL_INTERVAL_MS);
    expect(screen.getByText("Attempt 1")).toBeInTheDocument();
    expect(screen.getByText("RUNNING", { selector: ".state-badge" })).toBeInTheDocument();
  });
});

describe("logs", () => {
  it("shows empty and blank lines", async () => {
    renderPage();
    await flush();
    expect(screen.getByText("No log lines yet")).toBeInTheDocument();
    cleanup();
    logDefault = () => jsonResponse(200, logPage([{ ...lines(1, 1)[0], line: "" }]));
    renderPage();
    await flush();
    expect(screen.queryByText("No log lines yet")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Logs" })).not.toHaveTextContent("undefined");
    expect(document.querySelectorAll(".log-line")).toHaveLength(1);
  });

  it("appends new pages using after, without duplicates", async () => {
    logQueue.push(() => jsonResponse(200, logPage(lines(1, 3))));
    logQueue.push(() => jsonResponse(200, logPage(lines(4, 5))));
    logQueue.push(() => jsonResponse(200, logPage([], 5)));
    renderPage();
    await flush();
    expect(document.querySelectorAll(".log-line")).toHaveLength(3);
    await advance(LOG_POLL_INTERVAL_MS);
    expect(getLogs()[1]).toBe("/api/v1/jobs/j1/logs?after=3");
    const text = Array.from(document.querySelectorAll(".log-line")).map((e) => e.textContent);
    expect(text).toHaveLength(5);
    expect(text[4]).toContain("line 5");
    await advance(LOG_POLL_INTERVAL_MS);
    expect(getLogs()[2]).toBe("/api/v1/jobs/j1/logs?after=5");
    expect(document.querySelectorAll(".log-line")).toHaveLength(5);
  });

  it("does not re-add lines at or below the highest seq", async () => {
    logQueue.push(() => jsonResponse(200, logPage(lines(1, 3))));
    logQueue.push(() => jsonResponse(200, logPage(lines(2, 4))));
    renderPage();
    await flush();
    await advance(LOG_POLL_INTERVAL_MS);
    expect(document.querySelectorAll(".log-line")).toHaveLength(4);
  });

  it("follows has_more at once across pages", async () => {
    logQueue.push(() => jsonResponse(200, logPage(lines(1, 500), 500, true)));
    logQueue.push(() => jsonResponse(200, logPage(lines(501, 1000), 1000, true)));
    logQueue.push(() => jsonResponse(200, logPage(lines(1001, 1200), 1200, false)));
    renderPage();
    await flush();
    expect(getLogs()).toEqual([
      "/api/v1/jobs/j1/logs",
      "/api/v1/jobs/j1/logs?after=500",
      "/api/v1/jobs/j1/logs?after=1000",
    ]);
    expect(document.querySelectorAll(".log-line")).toHaveLength(1200);
  });

  it("keeps lines from several attempts in one list", async () => {
    logDefault = () =>
      jsonResponse(200, logPage([...lines(1, 1), ...lines(2, 2, { attempt_number: 2 })]));
    renderPage();
    await flush();
    const rows = Array.from(document.querySelectorAll(".log-line")).map((e) => e.textContent);
    expect(rows[0]).toContain("#1");
    expect(rows[1]).toContain("#2");
  });

  it("shows a logs failure in the Logs section, keeps lines, retries with same after", async () => {
    logQueue.push(() => jsonResponse(200, logPage(lines(1, 2))));
    logQueue.push(() => jsonResponse(500, envelope("internal_error", "logs broke")));
    renderPage();
    await flush();
    await advance(LOG_POLL_INTERVAL_MS);
    const logs = screen.getByRole("region", { name: "Logs" });
    expect(within(logs).getByRole("alert")).toHaveTextContent("logs broke");
    expect(document.querySelectorAll(".log-line")).toHaveLength(2);
    expect(screen.getByText("nightly")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    await advance(LOG_POLL_INTERVAL_MS);
    expect(getLogs()[1]).toBe("/api/v1/jobs/j1/logs?after=2");
    expect(getLogs()[2]).toBe("/api/v1/jobs/j1/logs?after=2");
    expect(within(logs).queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("polling", () => {
  it("refreshes the job and stops after the terminal drain", async () => {
    jobQueue.push(() => jsonResponse(200, makeJob({ attempts: [makeAttempt()] })));
    jobDefault = () =>
      jsonResponse(
        200,
        makeJob({
          state: "SUCCEEDED",
          attempts: [
            makeAttempt({
              state: "SUCCEEDED",
              finished_at: "2026-10-26T10:00:10Z",
            }),
          ],
        }),
      );
    logQueue.push(() => jsonResponse(200, logPage(lines(1, 1))));
    logQueue.push(() => jsonResponse(200, logPage(lines(2, 3), 3, true)));
    logQueue.push(() => jsonResponse(200, logPage(lines(4, 4))));
    renderPage();
    await flush();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    await advance(LOG_POLL_INTERVAL_MS);
    expect(screen.getByText("SUCCEEDED", { selector: ".state-badge" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(document.querySelectorAll(".log-line")).toHaveLength(4);
    const before = fetchMock.mock.calls.length;
    await advance(10_000);
    expect(fetchMock.mock.calls.length).toBe(before);
  });

  it("fetches a terminal job once and sends nothing more", async () => {
    jobDefault = () => jsonResponse(200, makeJob({ state: "FAILED" }));
    renderPage();
    await flush();
    expect(getJobs()).toHaveLength(1);
    expect(getLogs()).toHaveLength(1);
    await advance(10_000);
    expect(getJobs()).toHaveLength(1);
    expect(getLogs()).toHaveLength(1);
  });

  it("does not overlap job requests", async () => {
    jobQueue.push(
      () => new Promise<Response>((r) => setTimeout(() => r(jsonResponse(200, makeJob())), 10_000)),
    );
    renderPage();
    await advance(5000);
    expect(getJobs()).toHaveLength(1);
    await advance(5000);
    expect(getJobs()).toHaveLength(1);
    await advance(LOG_POLL_INTERVAL_MS);
    expect(getJobs()).toHaveLength(2);
  });

  it("keeps data on a refresh failure, then clears the alert", async () => {
    jobQueue.push(() => jsonResponse(200, makeJob()));
    jobQueue.push(() => jsonResponse(500, envelope("internal_error", "job broke")));
    renderPage();
    await flush();
    await advance(LOG_POLL_INTERVAL_MS);
    expect(screen.getByRole("alert")).toHaveTextContent("job broke");
    expect(screen.getByText("nightly")).toBeInTheDocument();
    await advance(LOG_POLL_INTERVAL_MS);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("aborts and stops on unmount", async () => {
    const signals: AbortSignal[] = [];
    jobDefault = (init) => {
      signals.push(init.signal as AbortSignal);
      return new Promise<Response>(() => {});
    };
    const { unmount } = renderPage();
    await flush();
    unmount();
    expect(signals[0].aborted).toBe(true);
    const before = fetchMock.mock.calls.length;
    await advance(10_000);
    expect(fetchMock.mock.calls.length).toBe(before);
  });

  it("restarts from scratch when the route id changes", async () => {
    jobDefault = (init) => {
      void init;
      return jsonResponse(200, makeJob({ id: "a", name: "job-a" }));
    };
    logQueue.push(() => jsonResponse(200, logPage(lines(1, 2))));
    let resolveA: (r: Response) => void = () => {};
    renderPage("/jobs/a");
    await flush();
    expect(screen.getByText("job-a")).toBeInTheDocument();
    const signalsBefore = fetchMock.mock.calls.length;
    jobQueue.push(() => jsonResponse(200, makeJob({ id: "b", name: "job-b" })));
    await act(async () => {
      await userEvent
        .setup({ advanceTimers: vi.advanceTimersByTime })
        .click(screen.getByRole("button", { name: "go b" }));
    });
    await flush();
    void resolveA;
    expect(screen.getByText("job-b")).toBeInTheDocument();
    expect(screen.queryByText("job-a")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".log-line")).toHaveLength(0);
    const fresh = fetchMock.mock.calls.slice(signalsBefore).map((c) => c[0] as string);
    expect(fresh).toContain("/api/v1/jobs/b");
    expect(fresh).toContain("/api/v1/jobs/b/logs");
  });
});

describe("buttons", () => {
  const EXPECTED: Record<JobState, string[]> = {
    QUEUED: ["Cancel"],
    RUNNING: ["Cancel"],
    STALLED: ["Requeue", "Cancel"],
    FAILED: ["Requeue"],
    SUCCEEDED: [],
    CANCELLED: [],
  };

  it.each(JOB_STATES)("shows the right buttons for %s", async (state) => {
    jobDefault = () => jsonResponse(200, makeJob({ state }));
    renderPage();
    await flush();
    for (const name of ["Requeue", "Cancel"]) {
      const present = screen.queryByRole("button", { name }) !== null;
      expect(present).toBe(EXPECTED[state].includes(name));
    }
  });

  function setup() {
    return userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  }

  it("requeues once, disables both buttons while in flight, shows QUEUED and refreshes", async () => {
    jobDefault = () => jsonResponse(200, makeJob({ state: "STALLED", attempts: [makeAttempt()] }));
    let release: (r: Response) => void = () => {};
    postQueue.push(() => new Promise<Response>((r) => (release = r)));
    renderPage();
    await flush();
    const user = setup();
    const button = screen.getByRole("button", { name: "Requeue" });
    await user.click(button);
    await user.click(button);
    expect(posts()).toEqual(["/api/v1/jobs/j1/requeue"]);
    expect(screen.getByRole("button", { name: "Requeue" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    jobQueue.push(() =>
      jsonResponse(
        200,
        makeJob({
          state: "QUEUED",
          attempt_count: 0,
          attempts: [makeAttempt()],
        }),
      ),
    );
    const jobsBefore = getJobs().length;
    const logsBefore = getLogs().length;
    await act(async () => {
      release(jsonResponse(200, makeJob({ state: "QUEUED", attempt_count: 0 })));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("QUEUED", { selector: ".state-badge" })).toBeInTheDocument();
    expect(screen.getByText("0/3")).toBeInTheDocument();
    expect(getJobs().length).toBe(jobsBefore + 1);
    expect(getLogs().length).toBe(logsBefore + 1);
    expect(screen.getByText("Attempt 1")).toBeInTheDocument();
  });

  it("restarts polling after requeue from FAILED", async () => {
    jobQueue.push(() => jsonResponse(200, makeJob({ state: "FAILED" })));
    postQueue.push(() => jsonResponse(200, makeJob({ state: "QUEUED", attempt_count: 0 })));
    jobDefault = () => jsonResponse(200, makeJob({ state: "QUEUED", attempt_count: 0 }));
    renderPage();
    await flush();
    await setup().click(screen.getByRole("button", { name: "Requeue" }));
    await flush();
    const n = getJobs().length;
    await advance(LOG_POLL_INTERVAL_MS);
    expect(getJobs().length).toBe(n + 1);
  });

  it("cancels at once and drains logs", async () => {
    logQueue.push(() => jsonResponse(200, logPage(lines(1, 1))));
    postQueue.push(() => jsonResponse(200, makeJob({ state: "CANCELLED" })));
    jobQueue.push(() => jsonResponse(200, makeJob()));
    jobQueue.push(() => jsonResponse(200, makeJob({ state: "CANCELLED" })));
    logQueue.push(() => jsonResponse(200, logPage(lines(2, 2))));
    renderPage();
    await flush();
    await setup().click(screen.getByRole("button", { name: "Cancel" }));
    await flush();
    expect(posts()).toEqual(["/api/v1/jobs/j1/cancel"]);
    expect(screen.getByText("CANCELLED", { selector: ".state-badge" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(document.querySelectorAll(".log-line")).toHaveLength(2);
    const n = fetchMock.mock.calls.length;
    await advance(10_000);
    expect(fetchMock.mock.calls.length).toBe(n);
  });

  it("shows a 409 message, refreshes, re-enables, and does not retry", async () => {
    jobQueue.push(() => jsonResponse(200, makeJob({ state: "FAILED" })));
    jobQueue.push(() => jsonResponse(200, makeJob({ state: "QUEUED" })));
    postQueue.push(() => jsonResponse(409, envelope("conflict", "Job is not failed")));
    renderPage();
    await flush();
    await setup().click(screen.getByRole("button", { name: "Requeue" }));
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("Job is not failed");
    expect(posts()).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Requeue" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it.each([
    ["404", () => jsonResponse(404, envelope("not_found", "Job not found")), "Job not found"],
    ["500", () => jsonResponse(500, envelope("internal_error", "kaboom")), "kaboom"],
    [
      "502 html",
      () => textResponse(502, "<html>bad</html>", "text/html"),
      "Request failed with status 502",
    ],
  ])("shows the client message for a %s from an action", async (_n, respond, message) => {
    postQueue.push(respond);
    renderPage();
    await flush();
    await setup().click(screen.getByRole("button", { name: "Cancel" }));
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(screen.getByText("nightly")).toBeInTheDocument();
  });

  it("shows a network failure and clears the error on the next click", async () => {
    postQueue.push(() => Promise.reject(new TypeError("fail")) as unknown as Response);
    let release: (r: Response) => void = () => {};
    postQueue.push(() => new Promise<Response>((r) => (release = r)));
    renderPage();
    await flush();
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not reach the server");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => release(jsonResponse(200, makeJob({ state: "CANCELLED" }))));
  });

  it("shows the action alert and the refresh alert together", async () => {
    jobQueue.push(() => jsonResponse(200, makeJob()));
    jobQueue.push(() => jsonResponse(500, envelope("internal_error", "refresh broke")));
    postQueue.push(() => jsonResponse(500, envelope("internal_error", "action broke")));
    renderPage();
    await flush();
    await setup().click(screen.getByRole("button", { name: "Cancel" }));
    await flush();
    expect(screen.getAllByRole("alert").map((a) => a.textContent)).toEqual([
      "refresh broke",
      "action broke",
    ]);
  });
});
