export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export interface Job {
  id: number;
  kind: "ingest" | "analyze";
  status: JobStatus;
  source_path: string | null;
  series_id: number | null;
  batch_id: number | null;
  batch_row: number | null;
  parent_job_id: number | null;
  submitted_by: string;
  attempts: number;
  max_attempts: number;
  error_code: string | null;
  error_message: string | null;
  result: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface SagittalGeometry {
  z_top: number;
  z_bottom: number;
  height: number;
  width: number;
  mm_per_pixel_z: number;
  mm_per_pixel_y: number;
  column_index: number;
}

export interface Series {
  id: number;
  series_instance_uid: string;
  status: "indexed" | "ready" | "analysis_failed";
  source_path: string;
  modality: string | null;
  series_description: string | null;
  series_number: number | null;
  rows: number;
  columns: number;
  num_slices: number;
  pixel_spacing_row: number | null;
  pixel_spacing_col: number | null;
  slice_thickness: number | null;
  spacing_between_slices: number | null;
  computed_slice_spacing: number;
  z_min: number;
  z_max: number;
  attributes: Record<string, unknown>;
  warnings: string[];
  study_id: number;
  study_instance_uid: string;
  patient_id: string | null;
  study_description: string | null;
  study_date: string | null;
  created_at: string;
}

export interface SeriesDetail extends Series {
  analysis: {
    job_id: number;
    sagittal_geometry: SagittalGeometry;
    qc: Record<string, number | null>;
    created_at: string;
  } | null;
  last_analysis_job: Pick<Job, "id" | "status" | "error_code" | "error_message" | "attempts" | "max_attempts"> | null;
}

export interface Located {
  index: number;
  z: number;
  instance_number: number | null;
  num_slices: number;
  requested_z: number | null;
  distance_mm: number;
  outside_volume: boolean;
  sagittal_fraction: number;
}

export interface Batch {
  id: number;
  filename: string | null;
  submitted_by: string;
  total_rows: number;
  created_at: string;
  rows_ready: number;
  rows_failed: number;
  rows_pending: number;
  done: boolean;
  queued?: number;
  rejected?: number;
}

export interface LibraryDirectory {
  path: string;
  absolute_path: string;
  file_count: number;
  series_id: number | null;
}
