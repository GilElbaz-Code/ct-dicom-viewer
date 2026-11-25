import type { SeriesDetail } from "../src/types";

export function series(id = 1): SeriesDetail {
  return {
    id, series_instance_uid: `1.2.${id}`, study_instance_uid: `1.3.${id}`, study_id: id,
    status: "ready", source_path: "/data/example", modality: "CT",
    patient_id: `patient-${id}`, study_description: `study-${id}`, series_description: `series-${id}`,
    series_number: 1, rows: 64, columns: 64, num_slices: 3,
    pixel_spacing_row: 1, pixel_spacing_col: 1, slice_thickness: 1,
    spacing_between_slices: null, computed_slice_spacing: 2.5, z_min: 0, z_max: 5,
    attributes: {}, warnings: [], study_date: null,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    analysis: null, last_analysis_job: null,
    slices: [0, 1, 5].map((z, index) => ({ slice_index: index, z_position: z, instance_number: index + 1 })),
  };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
