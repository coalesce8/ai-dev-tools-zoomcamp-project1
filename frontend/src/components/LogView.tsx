import { useLayoutEffect, useRef } from "react";
import type { LogLine } from "../api/types";
import ErrorMessage from "./ErrorMessage";

const BOTTOM_SLACK_PX = 4;

interface Props {
  lines: LogLine[];
  error: unknown;
}

export default function LogView({ lines, error }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);

  // Follow the bottom only when the user was already there.
  useLayoutEffect(() => {
    const el = box.current;
    if (el && atBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [lines]);

  const onScroll = () => {
    const el = box.current;
    if (el) {
      atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK_PX;
    }
  };

  return (
    <section aria-label="Logs" className="detail-section">
      <h2>Logs</h2>
      <ErrorMessage error={error} />
      {lines.length === 0 ? (
        <p>No log lines yet</p>
      ) : (
        <div className="log-box" ref={box} onScroll={onScroll}>
          <pre className="log-lines">
            {lines.map((l) => (
              <span className="log-line" key={l.seq}>
                <span className="log-attempt">#{l.attempt_number}</span> <span>{l.ts}</span>{" "}
                <span>{l.line}</span>
              </span>
            ))}
          </pre>
        </div>
      )}
    </section>
  );
}
