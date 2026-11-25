import { type FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { usePolling } from "../hooks";
import type { Batch, LibraryDirectory } from "../types";
import { ErrorBox, timeAgo } from "./common";
import { JobsTable } from "./JobsTable";
import { uploadFolderFiles, type UploadProgress } from "../upload";

export function ImportPanel({ onOpenSeries }: { onOpenSeries: (id: number) => void }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = () => setRefreshKey((k) => k + 1);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [batchFilter, setBatchFilter] = useState<number | null>(null);

  const [dirs, setDirs] = useState<LibraryDirectory[]>([]);
  const [manualPath, setManualPath] = useState("");
  const [batches, setBatches] = useState<Batch[]>([]);
  const [batchCursors, setBatchCursors] = useState<number[]>([]);
  const [csv, setCsv] = useState<File | null>(null);
  const [upload, setUpload] = useState<UploadProgress>(null);
  const uploadController = useRef<AbortController | null>(null);
  useEffect(() => () => uploadController.current?.abort(), []);

  usePolling(async (signal) => {
    try {
      const directories = await api.libraryDirectories(signal);
      if (!signal.aborted) setDirs(directories);
    } catch (e) {
      if (!signal.aborted) setError((e as Error).message);
    }
  }, null, [refreshKey]);

  const batchesActive = batches.some((b) => !b.done);
  usePolling(
    async (signal) => {
      try {
        const b = await api.listBatches(batchCursors.at(-1), signal);
        if (signal.aborted) return;
        setBatches(b);
      } catch (e) {
        if (!signal.aborted) setError((e as Error).message);
      }
    },
    batchesActive ? 2000 : 15000,
    [refreshKey, batchCursors],
  );

  async function run(action: () => Promise<string>) {
    setError(null);
    setMessage(null);
    try {
      setMessage(await action());
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const indexPath = (path: string) =>
    run(async () => {
      const job = await api.submitPath(path);
      return `Job #${job.id} queued for ${path}`;
    });

  function submitManual(event: FormEvent) {
    event.preventDefault();
    if (manualPath.trim()) void indexPath(manualPath.trim());
  }

  function submitCsv(event: FormEvent) {
    event.preventDefault();
    if (!csv) return;
    void run(async () => {
      const batch = await api.submitBatch(csv);
      setBatchFilter(batch.id);
      setBatchCursors([]);
      return `Batch #${batch.id}: ${batch.queued} row(s) queued, ${batch.rejected} rejected (see the jobs below)`;
    });
  }

  function uploadFolder(fileList: FileList | null) {
    const files = Array.from(fileList ?? []).filter((f) => !f.name.startsWith("."));
    if (files.length === 0) return;
    void run(async () => {
      const controller = new AbortController();
      uploadController.current = controller;
      try {
        const job = await uploadFolderFiles(files, setUpload, controller);
        return `Uploaded ${files.length} files; job #${job.id} queued`;
      } finally {
        uploadController.current = null;
      }
    });
  }

  return (
    <section className="import">
      {message && <div className="ok-box">{message}</div>}
      <ErrorBox>{error}</ErrorBox>

      <div className="import-grid">
        <div className="card">
          <h3>Index a directory on the server</h3>
          <button className="small" onClick={refresh}>Refresh directories</button>
          <p className="muted">Directories under the mounted DICOM library (the <code>data/</code> folder).</p>
          <table className="grid compact">
            <tbody>
              {dirs.length === 0 && (
                <tr>
                  <td className="muted">No directories found in the library.</td>
                </tr>
              )}
              {dirs.map((d) => (
                <tr key={d.absolute_path}>
                  <td className="mono path">{d.path}</td>
                  <td>{d.file_count} files</td>
                  <td className="actions">
                    {d.series_id && (
                      <button className="small" onClick={() => onOpenSeries(d.series_id!)}>
                        View
                      </button>
                    )}
                    <button className="small primary" onClick={() => indexPath(d.path)}>
                      {d.series_id ? "Re-index" : "Index"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <form className="row" onSubmit={submitManual}>
            <input
              value={manualPath}
              onChange={(e) => setManualPath(e.target.value)}
              placeholder="or type a path, e.g. study_1/series_2"
              className="grow"
            />
            <button type="submit">Index path</button>
          </form>
        </div>

        <div className="card">
          <h3>Upload a DICOM folder</h3>
          <p className="muted">Choose a folder on your computer; files are sent in small parts, then indexed.</p>
          <input
            type="file"
            multiple
            disabled={upload !== null}
            onChange={(e) => uploadFolder(e.target.files)}
            {...({ webkitdirectory: "" } as Record<string, string>)}
          />
          {upload && (
            <div className="progress">
              <div style={{ width: `${(100 * upload.done) / upload.total}%` }} />
              <span>
                {upload.done} / {upload.total} files
              </span>
            </div>
          )}

          <h3 className="spaced">Batch from CSV</h3>
          <p className="muted">
            One DICOM directory per row (a <code>path</code> header is optional). Each row becomes its own job.
          </p>
          <form className="row" onSubmit={submitCsv}>
            <input type="file" accept=".csv,text/csv" onChange={(e) => setCsv(e.target.files?.[0] ?? null)} />
            <button type="submit" className="primary" disabled={!csv}>
              Submit batch
            </button>
          </form>
          {batches.length > 0 && (
            <table className="grid compact">
              <thead>
                <tr>
                  <th>Batch</th>
                  <th>File</th>
                  <th>Rows</th>
                  <th>Ready</th>
                  <th>Failed</th>
                  <th>Submitted</th>
                </tr>
              </thead>
              <tbody>
                {batches.slice(0, 8).map((b) => (
                  <tr key={b.id} className="clickable" onClick={() => setBatchFilter(b.id)}>
                    <td>#{b.id}</td>
                    <td className="mono">{b.filename}</td>
                    <td>{b.total_rows}</td>
                    <td>{b.rows_ready}</td>
                    <td className={b.rows_failed ? "error-text" : ""}>{b.rows_failed}</td>
                    <td>{b.done ? timeAgo(b.created_at) : "running…"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {(batchCursors.length > 0 || batches.length > 8) && <div className="row">
            <button disabled={batchCursors.length === 0} onClick={() => setBatchCursors(batchCursors.slice(0, -1))}>Previous batches</button>
            <span>Page {batchCursors.length + 1}</span>
            <button disabled={batches.length <= 8} onClick={() => setBatchCursors([...batchCursors, batches[7].id])}>Next batches</button>
          </div>}
        </div>
      </div>

      <JobsTable
        key={batchFilter ?? "all"}
        refreshKey={refreshKey}
        batchId={batchFilter}
        onClearBatch={() => setBatchFilter(null)}
        onOpenSeries={onOpenSeries}
      />
    </section>
  );
}
