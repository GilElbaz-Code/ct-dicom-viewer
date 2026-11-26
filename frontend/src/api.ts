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

export interface UploadFileSpec {
  name: string;
  size: number;
  sha256: string;
}

export interface UploadSession {
  id: string;
  name: string;
  status: "uploading" | "complete";
  job_id: number | null;
  expires_at: string;
  files: (UploadFileSpec & { uploaded: boolean; form: { url: string; fields: Record<string, string> } | null })[];
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    // Our errors carry a string; FastAPI's request validation errors carry a list.
    const detail = body?.detail;
    throw new Error(Array.isArray(detail) ? detail.map((d: { msg: string }) => d.msg).join("; ") : (detail ?? response.statusText));
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
  createUpload: (name: string, files: UploadFileSpec[]) => request<UploadSession>("/uploads", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, files }),
  }),
  uploadSession: (id: string) => request<UploadSession>(`/uploads/${id}`),
  completeUpload: (id: string) => request<Job>(`/uploads/${id}/complete`, { method: "POST" }),
  searchSeries: (filters: { max_slice_thickness?: number; description?: string }) =>
    request<Series[]>(`/series?${query(filters)}`),
  getSeries: (id: number) => request<Series>(`/series/${id}`),
  locate: (id: number, target: SliceTarget) => request<Located>(`/series/${id}/locate?${query(target)}`),
  directories: () => request<string[]>("/directories"),
  jobs: (batchId: string | null) => request<Job[]>(`/jobs?${query({ batch_id: batchId ?? undefined })}`),
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

// Images are plain <img> URLs. `v` changes when a series is re-indexed, so the browser never shows a stale cached image.
export const axialUrl = (series: Series, index: number, width: number, level: number) =>
  `/api/series/${series.id}/axial/${index}?${query({ window: width, level, v: series.updated_at })}`;

export const sagittalUrl = (series: Series, width: number, level: number) =>
  `/api/series/${series.id}/sagittal?${query({ window: width, level, v: series.updated_at })}`;
