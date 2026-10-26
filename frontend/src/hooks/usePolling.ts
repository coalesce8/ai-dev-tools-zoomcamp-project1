import { useCallback, useEffect, useRef } from "react";
import type { DependencyList } from "react";

// Runs tick(signal) at once and whenever deps change, then again intervalMs
// after each run settles (a timeout chain, so runs never overlap). A deps
// change or unmount aborts the in-flight run and cancels the pending timer.
// The loop never stops on an error: tick handles its own errors.
// Returns refresh(), which aborts any in-flight run and starts one now.
export function usePolling(
  tick: (signal: AbortSignal) => Promise<void>,
  intervalMs: number,
  deps: DependencyList,
): () => void {
  const tickRef = useRef(tick);
  const runRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    tickRef.current = tick;
  });

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let stopped = false;

    const run = async () => {
      clearTimeout(timer);
      controller?.abort();
      const current = new AbortController();
      controller = current;
      try {
        await tickRef.current(current.signal);
      } catch {
        // Errors are the caller's to show; the loop carries on.
      }
      if (stopped || current.signal.aborted) {
        return;
      }
      timer = setTimeout(run, intervalMs);
    };

    runRef.current = run;
    void run();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      runRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);

  return useCallback(() => runRef.current?.(), []);
}
