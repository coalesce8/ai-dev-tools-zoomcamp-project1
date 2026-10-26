import { JOB_STATES } from "../api/types";
import type { JobState } from "../api/types";
import { canCancel, canRequeue, isTerminal } from "./jobActions";

const EXPECTED: Record<JobState, [boolean, boolean, boolean]> = {
  QUEUED: [false, true, false],
  RUNNING: [false, true, false],
  STALLED: [true, true, false],
  FAILED: [true, false, true],
  SUCCEEDED: [false, false, true],
  CANCELLED: [false, false, true],
};

describe("jobActions", () => {
  it.each(JOB_STATES)("%s", (state) => {
    expect([canRequeue(state), canCancel(state), isTerminal(state)]).toEqual(EXPECTED[state]);
  });
});
