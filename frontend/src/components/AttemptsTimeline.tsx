import type { Attempt } from "../api/types";
import { formatSeconds } from "../format";

const NONE = "—";

function runTime(attempt: Attempt): string {
  if (attempt.finished_at === null) {
    return NONE;
  }
  const seconds = (Date.parse(attempt.finished_at) - Date.parse(attempt.started_at)) / 1000;
  return formatSeconds(seconds);
}

export default function AttemptsTimeline({ attempts }: { attempts: Attempt[] }) {
  return (
    <section aria-label="Attempts" className="detail-section">
      <h2>Attempts</h2>
      {attempts.length === 0 ? (
        <p>No attempts yet</p>
      ) : (
        <ol className="attempts">
          {attempts.map((a) => (
            <li key={a.id}>
              <h3>Attempt {a.number}</h3>
              <dl>
                <dt>State</dt>
                <dd>{a.state}</dd>
                <dt>Worker</dt>
                <dd>{a.worker_id ?? NONE}</dd>
                <dt>Started</dt>
                <dd>{a.started_at}</dd>
                <dt>Finished</dt>
                <dd>{a.finished_at ?? NONE}</dd>
                <dt>Run time</dt>
                <dd>{runTime(a)}</dd>
                {a.failure_cause !== null && (
                  <>
                    <dt>Failure cause</dt>
                    <dd>{a.failure_cause}</dd>
                  </>
                )}
                {a.exit_reason !== null && (
                  <>
                    <dt>Exit reason</dt>
                    <dd>{a.exit_reason}</dd>
                  </>
                )}
              </dl>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
