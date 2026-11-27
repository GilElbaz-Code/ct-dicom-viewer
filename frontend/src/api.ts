// Typed calls to the FastAPI backend. The types mirror backend/app/api/schemas.py.

export type JobStatus = "queued" | "running" | "done" | "failed";

export interface Job {
  id: number;
  path: string;
  batch_id: string | null;
  status: JobStatus;
  error: string | null;
  series_id: number | null;
  created_at: string;
  finished_at: string | null;
  attempt_count: number;
  stage: string;
}

export interface Series {
  id: number;
  series_uid: string;
  study_uid: string;
  patient_id: string | null;
  study_description: string | null;
  series_description: string | null;
  modality: string | null;
  pixel_spacing: [number, number] | null;
  slice_thickness: number | null;
  spacing_between_slices: number | null;
  slice_spacing: number;
  num_slices: number;
  rows: number;
  columns: number;
  z_min: number;
  z_max: number;
  source_path: string;
  updated_at: string;
  analysis_available: boolean;
}

export interface Located {
  index: number;
  z: number;
  num_slices: number;
  distance_mm: number;
  outside: boolean;
}

export type SliceTarget = { z: number } | { index: number };

// Lists come in pages, newest first. A full page means there may be more: ask again with its last id.
export const PAGE_SIZE = 100;
export const nextPage = (page: { id: number }[]) => (page.length === PAGE_SIZE ? page[page.length - 1].id : undefined);

// The window/level range the API accepts (Width and Level in backend/app/api/series.py).
export const WINDOW_LIMITS = { width: { min: 1, max: 10000 }, level: { min: -2000, max: 5000 } };

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

// 409: the request named an older version (`v`) of a series that was indexed again since.
export const isStale = (error: unknown) => error instanceof ApiError && error.status === 409;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    // Our errors carry a string; FastAPI's request validation errors carry a list.
    const detail = body?.detail;
    throw new ApiError(
      Array.isArray(detail) ? detail.map((d: { msg: string }) => d.msg).join("; ") : (detail ?? response.statusText),
      response.status,
    );
  }
  return body as T;
}

const query = (params: Record<string, string | number | undefined>) =>
  new URLSearchParams(
    Object.entries(params)
      .filter(([, value]) => value !== undefined && value !== "")
      .map(([key, value]) => [key, String(value)]),
  ).toString();

export const api = {
  searchSeries: (filters: { max_slice_thickness?: number; description?: string }, beforeId?: number) =>
    request<Series[]>(`/series?${query({ ...filters, before_id: beforeId, limit: PAGE_SIZE })}`),
  getSeries: (id: number) => request<Series>(`/series/${id}`),
  // `v` (updated_at as received, never re-formatted) makes the API answer 409 once the series is indexed again.
  locate: (series: Series, target: SliceTarget) =>
    request<Located>(`/series/${series.id}/locate?${query({ ...target, v: series.updated_at })}`),
  directories: () => request<string[]>("/directories"),
  jobs: (batchId: string | null, beforeId?: number) =>
    request<Job[]>(`/jobs?${query({ batch_id: batchId ?? undefined, before_id: beforeId, limit: PAGE_SIZE })}`),
  indexFolder: (path: string) =>
    request<Job>("/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),
  indexCsv: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Job[]>("/batches", { method: "POST", body: form });
  },
};

// Images are plain <img> URLs. `v` changes when a series is re-indexed, so the browser never shows a stale cached
// image, and the API rejects a URL of an older version (409) instead of mixing it with the new one.
export const axialUrl = (series: Series, index: number, width: number, level: number) =>
  `/api/series/${series.id}/axial/${index}?${query({ window: width, level, v: series.updated_at })}`;

// An <img> cannot read why it failed, so ask the same URL again for the API's error.
export const imageError = (url: string) =>
  request<unknown>(url.replace(/^\/api/, "")).then(
    () => new Error("The image could not be displayed"),
    (error: Error) => error,
  );

export const sagittalUrl = (series: Series, width: number, level: number) =>
  `/api/series/${series.id}/sagittal?${query({ window: width, level, v: series.updated_at })}`;
