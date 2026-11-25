import { expect, it, vi } from "vitest";
import { api, ApiError } from "../src/api";
import { PART_BYTES, PART_FILES, partEnd, uploadFolderFiles } from "../src/upload";
import { deferred } from "./fixtures";

it("clears progress when opening the session fails (unit; mocked API)", async () => {
  vi.spyOn(api, "startUpload").mockRejectedValue(new Error("offline"));
  const progress = vi.fn();
  await expect(uploadFolderFiles([new File(["x"], "a")], progress, new AbortController())).rejects.toThrow("offline");
  expect(progress).toHaveBeenLastCalledWith(null);
});

it("waits for the other senders before clearing progress after a failed part", async () => {
  vi.spyOn(api, "startUpload").mockResolvedValue({ upload_id: "test" });
  const pending = deferred<{ saved: number }>();
  let calls = 0;
  vi.spyOn(api, "uploadFiles").mockImplementation(() => {
    calls++;
    return calls === 1 ? Promise.reject(new ApiError(413, "too large")) : pending.promise;
  });
  const complete = vi.spyOn(api, "completeUpload");
  const progress = vi.fn();
  const controller = new AbortController();
  const task = uploadFolderFiles(Array.from({ length: 41 }, (_, i) => new File(["x"], String(i))), progress, controller);
  const outcome = expect(task).rejects.toThrow("too large");
  await vi.waitFor(() => expect(controller.signal.aborted).toBe(true));
  expect(progress.mock.calls.at(-1)?.[0]).not.toBeNull();
  pending.resolve({ saved: 20 });
  await outcome;
  expect(progress).toHaveBeenLastCalledWith(null);
  expect(complete).not.toHaveBeenCalled();
  expect(progress).toHaveBeenCalledTimes(2); // initial progress + cleanup; no late sender updates
});

it("retries a transient part failure with the same session and waits for completion", async () => {
  vi.spyOn(api, "startUpload").mockResolvedValue({ upload_id: "same-session" });
  const parts = vi.spyOn(api, "uploadFiles").mockRejectedValueOnce(new ApiError(503, "unavailable")).mockResolvedValue({ saved: 1 });
  const completion = deferred<Awaited<ReturnType<typeof api.completeUpload>>>();
  vi.spyOn(api, "completeUpload").mockReturnValue(completion.promise);
  const progress = vi.fn();
  const task = uploadFolderFiles([new File(["x"], "a")], progress, new AbortController());
  await vi.waitFor(() => expect(api.completeUpload).toHaveBeenCalled());
  expect(progress.mock.calls.at(-1)?.[0]).toEqual({ done: 1, total: 1 });
  completion.resolve({ id: 7 } as Awaited<ReturnType<typeof api.completeUpload>>);
  expect((await task).id).toBe(7);
  expect(parts.mock.calls.map((args) => args[0])).toEqual(["same-session", "same-session"]);
  expect(progress).toHaveBeenLastCalledWith(null);
});

it("caps each part by bytes as well as by file count, so no request exceeds the proxy limit", () => {
  const sized = (mb: number) => Object.defineProperty(new File([], "f"), "size", { value: mb * 1024 * 1024 });
  expect(partEnd(Array.from({ length: 45 }, () => sized(1)), 0)).toBe(PART_FILES);
  const large = Array.from({ length: 5 }, () => sized(40));
  expect(partEnd(large, 0)).toBe(2); // 80 MB; a third file would pass PART_BYTES
  expect(partEnd(large, 4)).toBe(5); // the last file alone
  expect(partEnd([sized(150)], 0)).toBe(1); // never an empty part; the server rejects oversized files itself
  expect(PART_BYTES).toBeLessThan(200 * 1024 * 1024); // nginx client_max_body_size
});
