// Hand-written from the API shapes groomed in #3, #5 and #6.
// Timestamps are ISO 8601 strings.

export type JobState = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "STALLED" | "CANCELLED";

export const JOB_STATES: readonly JobState[] = [
  "QUEUED",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
  "STALLED",
  "CANCELLED",
];

export type AttemptState = "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";

export type FailureCause = "workload" | "infra";

export type WorkloadMode = "normal" | "always_fail";

export interface WorkloadSpec {
  duration_s: number;
  fail_probability: number;
  mode: WorkloadMode;
}

export interface JobCreate {
  name: string;
  spec: {
    duration_s: number;
    fail_probability?: number;
    mode?: WorkloadMode;
  };
  max_attempts?: number;
}

export interface Job {
  id: string;
  name: string;
  spec: WorkloadSpec;
  state: JobState;
  max_attempts: number;
  attempt_count: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  last_error: string | null;
}

export interface Attempt {
  id: string;
  number: number;
  worker_id: string | null;
  state: AttemptState;
  lease_expires_at: string | null;
  started_at: string;
  finished_at: string | null;
  exit_reason: string | null;
  failure_cause: FailureCause | null;
}

export type JobDetail = Job & { attempts: Attempt[] };

export interface JobPage {
  items: Job[];
  next_cursor: string | null;
}

export interface LogLine {
  seq: number;
  attempt_number: number;
  ts: string;
  line: string;
}

export interface LogPage {
  items: LogLine[];
  next_after: number;
  has_more: boolean;
}
