import { apiFetch } from "./client";
import type { Job, JobCreate, JobDetail, JobPage, JobState, LogPage } from "./types";

export interface ListJobsParams {
  state?: JobState;
  limit?: number;
  cursor?: string;
}

function withQuery(path: string, params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      query.set(key, String(value));
    }
  }
  const qs = query.toString();
  return qs ? `${path}?${qs}` : path;
}

export function createJob(body: JobCreate, init: RequestInit = {}): Promise<Job> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  return apiFetch<Job>("/jobs", {
    ...init,
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

export function listJobs(params: ListJobsParams = {}, init?: RequestInit): Promise<JobPage> {
  const { state, limit, cursor } = params;
  return apiFetch<JobPage>(withQuery("/jobs", { state, limit, cursor }), init);
}

export function getJob(id: string, init?: RequestInit): Promise<JobDetail> {
  return apiFetch<JobDetail>(`/jobs/${encodeURIComponent(id)}`, init);
}

export function getJobLogs(id: string, after?: number, init?: RequestInit): Promise<LogPage> {
  return apiFetch<LogPage>(withQuery(`/jobs/${encodeURIComponent(id)}/logs`, { after }), init);
}
