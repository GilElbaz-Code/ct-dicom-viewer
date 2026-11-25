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
export function usePolling(fn: () => Promise<unknown> | void, intervalMs: number | null, deps: unknown[] = []) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    void saved.current();
    if (intervalMs === null) return;
    const t = setInterval(() => void saved.current(), intervalMs);
    return () => clearInterval(t);
    // `fn` is read through a ref, so only the interval and the explicit deps restart polling.
  }, [intervalMs, ...deps]);
}
