import { useEffect, useRef, useState } from "react";

/** Value that only updates after `delay` ms without changes (avoids one request per keystroke). */
export function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

/** Calls `fn` now and then every `intervalMs`; the interval can change between calls. */
export function usePolling(fn: (signal: AbortSignal) => Promise<unknown> | void, intervalMs: number | null, deps: unknown[] = []) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function tick() {
      try {
        await saved.current(controller.signal);
      } catch (error) {
        if (!controller.signal.aborted) console.error("Polling failed", error);
      } finally {
        if (!controller.signal.aborted && intervalMs !== null) timer = setTimeout(tick, intervalMs);
      }
    }
    void tick();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
    // `fn` is read through a ref, so only the interval and the explicit deps restart polling.
  }, [intervalMs, ...deps]);
}
