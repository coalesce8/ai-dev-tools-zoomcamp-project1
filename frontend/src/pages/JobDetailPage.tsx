import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router";
import { cancelJob, requeueJob } from "../api/actions";
import { ApiError } from "../api/client";
import { getJob, getJobLogs } from "../api/jobs";
import type { Attempt, Job, JobDetail, LogLine } from "../api/types";
import AttemptsTimeline from "../components/AttemptsTimeline";
import ErrorMessage from "../components/ErrorMessage";
import LogView from "../components/LogView";
import StateBadge from "../components/StateBadge";
import { formatSeconds } from "../format";
import { usePolling } from "../hooks/usePolling";
import { canCancel, canRequeue, isTerminal } from "./jobActions";
import "./JobDetailPage.css";

export const LOG_POLL_INTERVAL_MS = 2000;

const NONE = "—";

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function JobHeader({ job }: { job: Job }) {
  return (
    <section aria-label="Job" className="detail-section">
      <h2>{job.name}</h2>
      <StateBadge state={job.state} />
      <dl>
        <dt>ID</dt>
        <dd>{job.id}</dd>
        <dt>Attempts used</dt>
        <dd>
          {job.attempt_count}/{job.max_attempts}
        </dd>
        <dt>Requested duration</dt>
        <dd>{formatSeconds(job.spec.duration_s)}</dd>
        <dt>Fail probability</dt>
        <dd>{job.spec.fail_probability}</dd>
        <dt>Mode</dt>
        <dd>{job.spec.mode}</dd>
        <dt>Created</dt>
        <dd>{job.created_at}</dd>
        <dt>Started</dt>
        <dd>{job.started_at ?? NONE}</dd>
        <dt>Finished</dt>
        <dd>{job.finished_at ?? NONE}</dd>
        {job.last_error !== null && (
          <>
            <dt>Last error</dt>
            <dd>{job.last_error}</dd>
          </>
        )}
      </dl>
    </section>
  );
}

function JobDetail({ id }: { id: string }) {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [jobError, setJobError] = useState<unknown>(null);
  const [lines, setLines] = useState<LogLine[]>([]);
  const [logError, setLogError] = useState<unknown>(null);
  const [acting, setActing] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);

  // Highest log seq held; the next request sends it as `after`.
  const afterRef = useRef<number | undefined>(undefined);
  // Set once a terminal job's logs are drained, or the job is unknown.
  const doneRef = useRef(false);
  const actionAbort = useRef<AbortController | null>(null);

  const fetchLogs = async (signal: AbortSignal) => {
    try {
      for (;;) {
        const before = afterRef.current;
        const page = await getJobLogs(id, before, { signal });
        if (signal.aborted) {
          return;
        }
        const fresh = page.items.filter((l) => before === undefined || l.seq > before);
        const highest = fresh.reduce((m, l) => Math.max(m, l.seq), page.next_after);
        afterRef.current = Math.max(before ?? 0, highest);
        if (fresh.length > 0) {
          setLines((prev) => [...prev, ...fresh]);
        }
        setLogError(null);
        // Stop if the server says more but gave nothing new, to never spin.
        if (!page.has_more || afterRef.current === before) {
          return;
        }
      }
    } catch (err) {
      if (!signal.aborted && !isAbort(err)) {
        setLogError(err);
      }
    }
  };

  const refresh = usePolling(
    async (signal) => {
      if (doneRef.current) {
        return;
      }
      let latest: JobDetail | null = null;
      try {
        latest = await getJob(id, { signal });
        if (signal.aborted) {
          return;
        }
        setJob(latest);
        setJobError(null);
      } catch (err) {
        if (signal.aborted || isAbort(err)) {
          return;
        }
        setJobError(err);
        if (err instanceof ApiError && err.status === 404) {
          doneRef.current = true;
          return;
        }
      }
      await fetchLogs(signal);
      if (!signal.aborted && latest !== null && isTerminal(latest.state)) {
        doneRef.current = true;
      }
    },
    LOG_POLL_INTERVAL_MS,
    [id],
  );

  const act = async (run: (id: string, init: RequestInit) => Promise<Job>) => {
    setActing(true);
    setActionError(null);
    const controller = new AbortController();
    actionAbort.current = controller;
    try {
      const updated = await run(id, { signal: controller.signal });
      setJob((prev) => (prev ? { ...updated, attempts: prev.attempts } : prev));
    } catch (err) {
      if (isAbort(err)) {
        return;
      }
      setActionError(err);
    }
    setActing(false);
    // Fresh data either way: a 409 means the page was stale.
    doneRef.current = false;
    refresh();
  };

  // Abort an in-flight action when the page goes away.
  useEffect(() => () => actionAbort.current?.abort(), []);

  const attempts: Attempt[] = job?.attempts ?? [];

  return (
    <>
      <ErrorMessage error={jobError} />
      {job === null ? (
        jobError === null && <p>Loading…</p>
      ) : (
        <>
          <JobHeader job={job} />
          <div className="job-actions">
            {canRequeue(job.state) && (
              <button type="button" disabled={acting} onClick={() => void act(requeueJob)}>
                Requeue
              </button>
            )}
            {canCancel(job.state) && (
              <button type="button" disabled={acting} onClick={() => void act(cancelJob)}>
                Cancel
              </button>
            )}
            <ErrorMessage error={actionError} />
          </div>
          <AttemptsTimeline attempts={attempts} />
          <LogView lines={lines} error={logError} />
        </>
      )}
    </>
  );
}

export default function JobDetailPage() {
  // React Router decodes path params, so /jobs/a%20b gives "a b".
  const { id } = useParams();
  return (
    <section>
      <h1>Job</h1>
      {id !== undefined && <JobDetail key={id} id={id} />}
    </section>
  );
}
