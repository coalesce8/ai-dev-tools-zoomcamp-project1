import { render, screen, within } from "@testing-library/react";
import type { Stats } from "../api/stats";
import { JOB_STATES } from "../api/types";
import SummaryBar from "./SummaryBar";

const stats: Stats = {
  as_of: "2026-10-26T10:00:00+00:00",
  window_seconds: 600,
  counts: { QUEUED: 4, RUNNING: 1, SUCCEEDED: 3, FAILED: 2, STALLED: 0, CANCELLED: 7 },
  oldest_queued_age_seconds: 125,
  jobs_finished: 5,
  jobs_failed: 2,
  job_failure_rate: 0.4,
  attempts_finished: 8,
  attempts_failed_infra: 0,
  infra_attempt_failure_rate: 0,
};

describe("SummaryBar", () => {
  it("shows a count per state in order, including zeros", () => {
    render(<SummaryBar stats={stats} error={null} />);
    const bar = screen.getByRole("region", { name: "Summary" });
    const items = within(bar).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(
      JOB_STATES.map((s) => `${s} ${stats.counts[s]}`),
    );
  });

  it("shows oldest queued, both rates and the window", () => {
    render(<SummaryBar stats={stats} error={null} />);
    expect(screen.getByText("Oldest queued: 2m 5s")).toBeInTheDocument();
    expect(screen.getByText("Job failure rate: 40% (2 of 5)")).toBeInTheDocument();
    expect(screen.getByText("Infra attempt failure rate: 0% (0 of 8)")).toBeInTheDocument();
    expect(screen.getAllByText("last 10 min")).toHaveLength(2);
  });

  it("shows dashes for null values", () => {
    render(
      <SummaryBar
        stats={{
          ...stats,
          oldest_queued_age_seconds: null,
          jobs_finished: 0,
          jobs_failed: 0,
          job_failure_rate: null,
          attempts_finished: 0,
          infra_attempt_failure_rate: null,
        }}
        error={null}
      />,
    );
    expect(screen.getByText("Oldest queued: —")).toBeInTheDocument();
    expect(screen.getByText("Job failure rate: — (0 of 0)")).toBeInTheDocument();
    expect(screen.getByText("Infra attempt failure rate: — (0 of 0)")).toBeInTheDocument();
  });

  it("shows Loading… before the first response", () => {
    render(<SummaryBar stats={null} error={null} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });
});
