import type { Batch, Job, LibraryDirectory, Located, Series, SeriesDetail } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    // FastAPI returns {detail: string} or {detail: [{msg, loc}]} for validation errors
    const detail = body?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d: { msg: string; loc: string[] }) => `${d.loc.at(-1)}: ${d.msg}`).join("; ")
      : (detail ?? response.statusText);
    throw new ApiError(response.status, message, body?.code);
  }
  return body as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  searchSeries: (params: Record<string, string | number | undefined>) =>
    request<{ items: Series[]; total: number }>(`/series${query({ limit: 500, ...params })}`),
  getSeries: (id: number) => request<SeriesDetail>(`/series/${id}`),
  locate: (id: number, by: { z: number } | { index: number }) => request<Located>(`/series/${id}/locate${query(by)}`),
  axialUrl: (id: number, index: number, window: number, level: number) =>
    `/api/series/${id}/axial/${index}${query({ window, level })}`,
  sagittalUrl: (id: number, window: number, level: number, version: string) =>
    `/api/series/${id}/sagittal${query({ window, level, v: version })}`,

  listJobs: (params: Record<string, string | number | undefined>) => request<Job[]>(`/jobs${query(params)}`),
  submitPath: (path: string) => request<Job>("/jobs", json({ path })),
  retryJob: (id: number) => request<Job>(`/jobs/${id}/retry`, { method: "POST" }),

  submitBatch: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Batch>("/batches", { method: "POST", body: form });
  },
  listBatches: () => request<Batch[]>("/batches"),

  startUpload: () => request<{ upload_id: string }>("/uploads", { method: "POST" }),
  uploadFiles: (uploadId: string, files: File[]) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    return request<{ saved: number }>(`/uploads/${uploadId}/files`, { method: "POST", body: form });
  },
  completeUpload: (uploadId: string) => request<Job>(`/uploads/${uploadId}/complete`, { method: "POST" }),

  libraryDirectories: () => request<LibraryDirectory[]>("/library/directories"),
};
