import type { Stats } from "../api/stats";
import { JOB_STATES } from "../api/types";
import { formatRate, formatSeconds } from "../format";
import ErrorMessage from "./ErrorMessage";

function windowLabel(seconds: number): string {
  if (seconds > 0 && seconds % 60 === 0) {
    return `last ${seconds / 60} min`;
  }
  return `last ${formatSeconds(seconds)}`;
}

interface Props {
  stats: Stats | null;
  error: unknown;
}

export default function SummaryBar({ stats, error }: Props) {
  return (
    <section aria-label="Summary" className="summary-bar">
      <ErrorMessage error={error} />
      {stats === null ? (
        error === null || error === undefined ? (
          <p>Loading…</p>
        ) : null
      ) : (
        <>
          <ul className="summary-counts">
            {JOB_STATES.map((state) => (
              <li key={state}>
                <span>{state}</span> <strong>{stats.counts[state]}</strong>
              </li>
            ))}
          </ul>
          <p>
            Oldest queued:{" "}
            {stats.oldest_queued_age_seconds === null
              ? "—"
              : formatSeconds(stats.oldest_queued_age_seconds)}
          </p>
          <p>
            <span>
              Job failure rate: {formatRate(stats.job_failure_rate)} ({stats.jobs_failed} of{" "}
            {stats.jobs_finished})
            </span>{" "}
            <span className="summary-window">{windowLabel(stats.window_seconds)}</span>
          </p>
          <p>
            <span>
              Infra attempt failure rate: {formatRate(stats.infra_attempt_failure_rate)} (
              {stats.attempts_failed_infra} of {stats.attempts_finished})
            </span>{" "}
            <span className="summary-window">{windowLabel(stats.window_seconds)}</span>
          </p>
        </>
      )}
    </section>
  );
}
