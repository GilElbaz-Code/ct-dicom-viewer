import { api, ApiError } from "./api";

export type UploadProgress = { done: number; total: number } | null;

export const PART_FILES = 20;
export const PART_BYTES = 100 * 1024 * 1024; // well below nginx's 200 MB request limit

/** End (exclusive) of the part starting at `start`: up to PART_FILES files and PART_BYTES, at least one file. */
export function partEnd(files: File[], start: number): number {
  let end = start + 1;
  let bytes = files[start].size;
  while (end < files.length && end - start < PART_FILES && bytes + files[end].size <= PART_BYTES) bytes += files[end++].size;
  return end;
}

/** Retry only operations whose server contract is idempotent. */
async function retry<T>(action: () => Promise<T>, signal: AbortSignal): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    signal.throwIfAborted();
    try {
      return await action();
    } catch (error) {
      if (signal.aborted || attempt >= 2 || (error instanceof ApiError && error.status < 500 && ![408, 429].includes(error.status))) throw error;
      await new Promise<void>((resolve) => setTimeout(resolve, 300 * 2 ** attempt));
    }
  }
}

export async function uploadFolderFiles(files: File[], onProgress: (progress: UploadProgress) => void, controller: AbortController) {
  const { signal } = controller;
  const senders: Promise<void>[] = [];
  onProgress({ done: 0, total: files.length });
  try {
    const { upload_id } = await api.startUpload(signal);
    let next = 0;
    let done = 0;
    async function send() {
      while (next < files.length) {
        signal.throwIfAborted();
        const chunk = files.slice(next, partEnd(files, next));
        next += chunk.length;
        await retry(() => api.uploadFiles(upload_id, chunk, signal), signal);
        signal.throwIfAborted();
        done += chunk.length;
        onProgress({ done, total: files.length });
      }
    }
    senders.push(...Array.from({ length: 3 }, send));
    await Promise.all(senders);
    return await retry(() => api.completeUpload(upload_id, signal), signal);
  } catch (error) {
    controller.abort();
    throw error;
  } finally {
    await Promise.allSettled(senders);
    onProgress(null);
  }
}
