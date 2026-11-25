import { useState } from "react";
import { api } from "../api";
import { usePolling } from "../hooks";
import type { Job, JobStatus } from "../types";
import { ErrorBox, StatusBadge, duration, timeAgo } from "./common";

interface Props {
  refreshKey: number;
  batchId: number | null;
  onClearBatch: () => void;
  onOpenSeries: (id: number) => void;
}

/** Jobs live in the database, so this table looks the same after closing and reopening the browser. */
export function JobsTable({ refreshKey, batchId, onClearBatch, onOpenSeries }: Props) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [status, setStatus] = useState<JobStatus | "">("");
  const [error, setError] = useState<string | null>(null);
  // before_id of each page after the first; keyset pages stay put while new jobs arrive
  const [cursors, setCursors] = useState<number[]>([]);
  const [reload, setReload] = useState(0);
  const active = jobs.some((j) => j.status === "queued" || j.status === "running");

  async function load(signal: AbortSignal) {
    try {
      const result = await api.listJobs({ status: status || undefined, batch_id: batchId ?? undefined, limit: 51, before_id: cursors.at(-1) }, signal);
      if (signal.aborted) return;
      setJobs(result);
      setError(null);
    } catch (e) {
      if (!signal.aborted) setError((e as Error).message);
    }
  }

  usePolling(load, active ? 2000 : 10000, [refreshKey, status, batchId, cursors, reload]);

  async function retry(id: number) {
    try {
      await api.retryJob(id);
      setReload((value) => value + 1);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <h3>Jobs {batchId !== null && <span className="chip">batch #{batchId} <button className="link" onClick={onClearBatch}>×</button></span>}</h3>
        <label className="field inline">
          <span>Status</span>
          <select value={status} onChange={(e) => { setStatus(e.target.value as JobStatus | ""); setCursors([]); }}>
            <option value="">all</option>
            <option value="queued">queued</option>
            <option value="running">running</option>
            <option value="succeeded">succeeded</option>
            <option value="failed">failed</option>
          </select>
        </label>
        {active && <span className="muted">refreshing…</span>}
      </div>
      <ErrorBox>{error}</ErrorBox>
      <table className="grid jobs">
        <thead>
          <tr>
            <th>#</th>
            <th>Kind</th>
            <th>Status</th>
            <th>Input</th>
            <th>Batch row</th>
            <th>Tries</th>
            <th>Created</th>
            <th>Took</th>
            <th>Result / reason</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {jobs.length === 0 && (
            <tr>
              <td colSpan={10} className="muted">
                No jobs yet.
              </td>
            </tr>
          )}
          {jobs.slice(0, 50).map((j) => (
            <tr key={j.id} className={j.status === "failed" ? "row-failed" : ""}>
              <td>{j.id}</td>
              <td>{j.kind}</td>
              <td>
                <StatusBadge status={j.status} />
              </td>
              <td className="mono path" title={j.source_path ?? undefined}>
                {j.kind === "ingest" ? j.source_path : `series #${j.series_id} (from job #${j.parent_job_id})`}
              </td>
              <td>{j.batch_id ? `#${j.batch_id} · ${j.batch_row}` : "—"}</td>
              <td>
                {j.attempts}/{j.max_attempts || "—"}
              </td>
              <td title={j.created_at}>{timeAgo(j.created_at)}</td>
              <td>{duration(j.started_at, j.finished_at)}</td>
              <td className="reason">
                {j.error_code ? (
                  <span className={j.status === "failed" ? "error-text" : "warn-text"}>
                    <code>{j.error_code}</code> {j.error_message}
                    {j.status === "queued" && " (will retry)"}
                  </span>
                ) : j.status === "succeeded" && j.kind === "ingest" && j.result ? (
                  <span>
                    {String(j.result.num_slices)} slices
                    {Array.isArray(j.result.warnings) && j.result.warnings.length > 0 && (
                      <span className="warn-text"> · {(j.result.warnings as string[]).join("; ")}</span>
                    )}
                  </span>
                ) : null}
              </td>
              <td className="actions">
                {j.status === "failed" && (
                  <button className="small" onClick={() => retry(j.id)}>
                    Retry
                  </button>
                )}
                {j.status === "succeeded" && j.series_id && (
                  <button className="small" onClick={() => onOpenSeries(j.series_id!)}>
                    View
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button disabled={cursors.length === 0} onClick={() => setCursors(cursors.slice(0, -1))}>Previous jobs</button>
        <span>Page {cursors.length + 1}</span>
        <button disabled={jobs.length <= 50} onClick={() => setCursors([...cursors, jobs[49].id])}>Next jobs</button>
      </div>
    </div>
  );
}
