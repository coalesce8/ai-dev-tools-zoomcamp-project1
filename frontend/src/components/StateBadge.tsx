import type { JobState } from "../api/types";
import "./StateBadge.css";

export default function StateBadge({ state }: { state: JobState }) {
  return <span className={`state-badge state-${state.toLowerCase()}`}>{state}</span>;
}
