import type { SeriesDetail } from "../types";
import { mm } from "./common";

export function MetadataTable({ series }: { series: SeriesDetail }) {
  const spacingTag = series.spacing_between_slices;
  const rows: [string, string][] = [
    ["Patient ID (anonymized)", series.patient_id ?? "—"],
    ["Study description", series.study_description ?? "—"],
    ["Series description", series.series_description ?? "—"],
    ["Modality", series.modality ?? "—"],
    [
      "Pixel spacing (row × column)",
      series.pixel_spacing_row !== null
        ? `${Number(series.pixel_spacing_row.toFixed(4))} × ${Number(series.pixel_spacing_col!.toFixed(4))} mm`
        : "—",
    ],
    ["Slice thickness", mm(series.slice_thickness)],
    [
      "Spacing between slices",
      spacingTag !== null
        ? mm(spacingTag)
        : `${mm(series.computed_slice_spacing)} (computed; tag not in header)`,
    ],
    ["Number of slices", String(series.num_slices)],
    ["Rows", String(series.rows)],
    ["Columns", String(series.columns)],
  ];
  return (
    <table className="kv">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <th>{k}</th>
            <td>{v}</td>
          </tr>
        ))}
      </tbody>
      <tbody className="secondary">
        <tr>
          <th>z range</th>
          <td>
            {mm(series.z_min)} … {mm(series.z_max)}
          </td>
        </tr>
        <tr>
          <th>Study date</th>
          <td>{series.study_date ?? "—"}</td>
        </tr>
        <tr>
          <th>Source</th>
          <td className="mono">{series.source_path}</td>
        </tr>
      </tbody>
    </table>
  );
}

const QC_LABELS: Record<string, string> = {
  hu_min: "Min HU",
  hu_max: "Max HU",
  hu_median: "Median HU",
  z_coverage_mm: "Coverage (mm)",
  slice_gaps: "Slice gaps",
  body_volume_ml_estimate: "Body volume, rough (mL)",
};

export function AnalysisPanel({ series }: { series: SeriesDetail }) {
  const job = series.last_analysis_job;
  return (
    <div>
      {series.warnings.length > 0 && (
        <ul className="warnings">
          {series.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {series.analysis ? (
        <table className="kv">
          <tbody>
            {Object.entries(QC_LABELS).map(([key, label]) => (
              <tr key={key}>
                <th>{label}</th>
                <td>{series.analysis!.qc[key] ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : job?.status === "failed" ? (
        <p className="error-text">
          Analysis failed after {job.attempts} attempt(s): <code>{job.error_code}</code> {job.error_message}
        </p>
      ) : (
        <p className="muted">Analysis {job?.status ?? "pending"}…</p>
      )}
    </div>
  );
}
