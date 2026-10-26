import { apiFetch } from "./client";
import type { JobState } from "./types";

// The /stats body, as groomed in #11.
export interface Stats {
  as_of: string;
  window_seconds: number;
  counts: Record<JobState, number>;
  oldest_queued_age_seconds: number | null;
  jobs_finished: number;
  jobs_failed: number;
  job_failure_rate: number | null;
  attempts_finished: number;
  attempts_failed_infra: number;
  infra_attempt_failure_rate: number | null;
}

export function getStats(signal?: AbortSignal): Promise<Stats> {
  return apiFetch<Stats>("/stats", { signal });
}
