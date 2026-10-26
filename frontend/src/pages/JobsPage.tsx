import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { listJobs } from "../api/jobs";
import { getStats } from "../api/stats";
import type { Stats } from "../api/stats";
import { JOB_STATES } from "../api/types";
import type { Job, JobPage, JobState } from "../api/types";
import ErrorMessage from "../components/ErrorMessage";
import StateBadge from "../components/StateBadge";
import SummaryBar from "../components/SummaryBar";
import { ageSeconds, formatSeconds } from "../format";
import { usePolling } from "../hooks/usePolling";

export const REFRESH_INTERVAL_MS = 3000;

type Filter = JobState | null;

interface JobsState {
  filter: Filter;
  page: JobPage | null;
  error: unknown;
}

interface StatsState {
  stats: Stats | null;
  error: unknown;
}

function parseFilter(value: string | null): Filter {
  return JOB_STATES.find((s) => s === value) ?? null;
}

function JobsTable({ items }: { items: Job[] }) {
  const now = Date.now();
  return (
    <table>
      <thead>
        <tr>
          <th>State</th>
          <th>Name</th>
          <th>Attempts</th>
          <th>Age</th>
          <th>Duration</th>
        </tr>
      </thead>
      <tbody>
        {items.map((job) => (
          <tr key={job.id}>
            <td>
              <StateBadge state={job.state} />
            </td>
            <td>
              <Link to={`/jobs/${encodeURIComponent(job.id)}`}>{job.name}</Link>
            </td>
            <td>
              {job.attempt_count}/{job.max_attempts}
            </td>
            <td>{formatSeconds(ageSeconds(job.created_at, now))}</td>
            <td>{formatSeconds(job.spec.duration_s)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function JobsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filter = parseFilter(searchParams.get("state"));

  const [jobsState, setJobsState] = useState<JobsState>({ filter, page: null, error: null });
  const [statsState, setStatsState] = useState<StatsState>({ stats: null, error: null });

  // Rows and errors belong to the filter they were fetched under.
  const jobs: JobsState =
    jobsState.filter === filter ? jobsState : { filter, page: null, error: null };

  const refreshJobs = usePolling(
    async (signal) => {
      try {
        const page = await listJobs({ state: filter ?? undefined }, { signal });
        if (!signal.aborted) {
          setJobsState({ filter, page, error: null });
        }
      } catch (err) {
        if (signal.aborted) {
          return;
        }
        setJobsState((prev) => ({
          filter,
          page: prev.filter === filter ? prev.page : null,
          error: err,
        }));
      }
    },
    REFRESH_INTERVAL_MS,
    [filter],
  );

  const refreshStats = usePolling(
    async (signal) => {
      try {
        const stats = await getStats(signal);
        if (!signal.aborted) {
          setStatsState({ stats, error: null });
        }
      } catch (err) {
        if (!signal.aborted) {
          setStatsState((prev) => ({ stats: prev.stats, error: err }));
        }
      }
    },
    REFRESH_INTERVAL_MS,
    [],
  );

  // Fetches jobs and stats at once and restarts the timers (used by #15).
  const refresh = () => {
    refreshJobs();
    refreshStats();
  };
  void refresh; // #15 passes this to the submit form

  function onFilterChange(value: string) {
    const next = new URLSearchParams(searchParams);
    const parsed = parseFilter(value);
    if (parsed === null) {
      next.delete("state");
    } else {
      next.set("state", parsed);
    }
    setSearchParams(next);
  }

  const page = jobs.page;

  return (
    <section>
      <h1>Jobs</h1>
      {/* Submit form (#15) */}
      <SummaryBar stats={statsState.stats} error={statsState.error} />
      <label>
        State{" "}
        <select value={filter ?? ""} onChange={(e) => onFilterChange(e.target.value)}>
          <option value="">All</option>
          {JOB_STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>
      <ErrorMessage error={jobs.error} />
      {page === null ? (
        jobs.error === null ? (
          <p>Loading…</p>
        ) : null
      ) : page.items.length === 0 ? (
        <p>{filter === null ? "No jobs yet" : `No ${filter} jobs`}</p>
      ) : (
        <>
          <JobsTable items={page.items} />
          {page.next_cursor !== null && <p>Showing the newest {page.items.length} jobs</p>}
        </>
      )}
    </section>
  );
}
