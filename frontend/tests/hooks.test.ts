import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { usePolling } from "../src/hooks";
import { deferred } from "./fixtures";

afterEach(() => { cleanup(); vi.useRealTimers(); });

it("waits for completion before scheduling another poll (unit; mocked timing)", async () => {
  vi.useFakeTimers();
  const pending = deferred<void>();
  const task = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(undefined);
  renderHook(() => usePolling(task, 1000));
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(task).toHaveBeenCalledTimes(1);
  await act(async () => { pending.resolve(); await pending.promise; });
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(task).toHaveBeenCalledTimes(2);
});

it("aborts an obsolete poll when its resource changes and on unmount", () => {
  const signals: AbortSignal[] = [];
  const task = (signal: AbortSignal) => { signals.push(signal); return new Promise<void>(() => {}); };
  const { rerender, unmount } = renderHook(({ id }) => usePolling(task, 1000, [id]), { initialProps: { id: 1 } });
  rerender({ id: 2 });
  expect(signals[0].aborted).toBe(true);
  expect(signals[1].aborted).toBe(false);
  unmount();
  expect(signals[1].aborted).toBe(true);
});
