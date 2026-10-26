import type { JobState } from "../api/types";

const REQUEUE_STATES: ReadonlySet<JobState> = new Set<JobState>(["FAILED", "STALLED"]);
const CANCEL_STATES: ReadonlySet<JobState> = new Set<JobState>(["QUEUED", "RUNNING", "STALLED"]);
const TERMINAL_STATES: ReadonlySet<JobState> = new Set<JobState>([
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
]);

export const canRequeue = (state: JobState): boolean => REQUEUE_STATES.has(state);
export const canCancel = (state: JobState): boolean => CANCEL_STATES.has(state);
export const isTerminal = (state: JobState): boolean => TERMINAL_STATES.has(state);
