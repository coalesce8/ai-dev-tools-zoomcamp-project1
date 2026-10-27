import { apiFetch } from "./client";
import type { Job } from "./types";

function post(id: string, action: "requeue" | "cancel", init: RequestInit = {}): Promise<Job> {
  return apiFetch<Job>(`/jobs/${encodeURIComponent(id)}/${action}`, {
    ...init,
    method: "POST",
  });
}

export function requeueJob(id: string, init?: RequestInit): Promise<Job> {
  return post(id, "requeue", init);
}

export function cancelJob(id: string, init?: RequestInit): Promise<Job> {
  return post(id, "cancel", init);
}
