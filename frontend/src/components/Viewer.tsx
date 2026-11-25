import { type FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { useDebounced, usePolling } from "../hooks";
import { locateSlice, type SliceLocation } from "../location";
import type { Series, SeriesDetail } from "../types";
import { ErrorBox, StatusBadge, mm } from "./common";
import { AnalysisPanel, MetadataTable } from "./SeriesInfo";

const PRESETS = [
  { name: "Soft tissue", width: 400, level: 40 },
  { name: "Lung", width: 1500, level: -600 },
  { name: "Bone", width: 1800, level: 400 },
  { name: "Brain", width: 80, level: 40 },
  { name: "Liver", width: 150, level: 30 },
];

type Mode = "mm" | "index";

/** Reading-room style text on the image: corners TL, TR, BL, BR; orientation letters top, right, bottom, left. */
function Overlay({ corners, markers }: { corners: (string[] | null)[]; markers: string[] }) {
  return (
    <div className="overlay" aria-hidden="true">
      {corners.map((lines, i) =>
        lines ? (
          <div key={i} className={`corner c${i}`}>
            {lines.filter(Boolean).map((l, j) => (
              <div key={j} className={i === 3 && j === 1 ? "z" : undefined}>
                {l}
              </div>
            ))}
          </div>
        ) : null,
      )}
      {markers.map((m, i) => (
        <span key={i} className={`marker m${i}`}>
          {m}
        </span>
      ))}
    </div>
  );
}

interface Props {
  seriesId: number | null;
  onSelectSeries: (id: number) => void;
}

export function Viewer({ seriesId, onSelectSeries }: Props) {
  const [seriesList, setSeriesList] = useState<Series[]>([]);
  const [loadedSeries, setSeries] = useState<SeriesDetail | null>(null);
  const series = loadedSeries?.id === seriesId ? loadedSeries : null;
  const [listOffset, setListOffset] = useState(0);
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [mode, setMode] = useState<Mode>("mm");
  const [input, setInput] = useState("");
  const [selection, setSelection] = useState<{ seriesId: number; by: SliceLocation } | null>(null);
  const [locateError, setLocateError] = useState<string | null>(null);
  let by: SliceLocation = { index: Math.floor((series?.num_slices ?? 0) / 2) };
  if (series && selection?.seriesId === series.id) {
    by = "index" in selection.by
      ? { index: Math.min(selection.by.index, series.slices.length - 1) } : selection.by;
  }
  const located = series?.slices.length ? locateSlice(series, by) : null;

  const [width, setWidth] = useState(400);
  const [level, setLevel] = useState(40);
  // Debounce the two numbers separately: a fresh {width, level} object on every render would never settle.
  const debouncedWidth = useDebounced(width, 200);
  const debouncedLevel = useDebounced(level, 200);
  const windowing = { width: debouncedWidth, level: debouncedLevel };

  useEffect(() => {
    const controller = new AbortController();
    api
      .searchSeries({ offset: listOffset }, controller.signal)
      .then((r) => {
        if (controller.signal.aborted) return;
        setSeriesList(r.items);
        setTotal(r.total);
        if (seriesId === null && r.items.length > 0) onSelectSeries(r.items[0].id);
      })
      .catch((e) => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [listOffset, seriesId]);

  // Load the series; keep polling while its analysis (sagittal view) is still pending.
  const analysisPending = series !== null && series.id === seriesId && series.status === "indexed";
  usePolling(
    async (signal) => {
      if (seriesId === null) return;
      try {
        const result = await api.getSeries(seriesId, signal);
        if (signal.aborted) return;
        setSeries(result);
        setError(null);
      } catch (e) {
        if (!signal.aborted) setError((e as Error).message);
      }
    },
    analysisPending ? 2000 : null,
    [seriesId],
  );

  function goTo(by: SliceLocation) {
    if (!series) return;
    try {
      locateSlice(series, by); // validate before updating the selection
      setSelection({ seriesId: series.id, by });
      setLocateError(null);
    } catch (e) {
      setLocateError((e as Error).message);
    }
  }

  useEffect(() => {
    setInput("");
    setLocateError(null);
    setError(null);
  }, [seriesId]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = Number(input);
    if (input.trim() === "" || !Number.isFinite(value)) {
      setLocateError(mode === "mm" ? "Enter a location in mm" : "Enter a slice index");
      return;
    }
    if (mode === "index" && !Number.isInteger(value)) {
      setLocateError("Slice index must be a whole number");
      return;
    }
    void goTo(mode === "mm" ? { z: value } : { index: value });
  }

  function onSlider(index: number) {
    setInput(mode === "index" ? String(index) : "");
    void goTo({ index });
  }

  function switchMode(next: Mode) {
    setMode(next);
    if (located) setInput(next === "index" ? String(located.index) : String(Number(located.z.toFixed(2))));
  }

  const [axialFailed, setAxialFailed] = useState<string | null>(null);
  const axialSrc =
    series && located
      ? api.axialUrl(series.id, located.index, windowing.width, windowing.level, series.updated_at)
      : null;

  const preset = PRESETS.find((p) => p.width === width && p.level === level)?.name ?? "custom";
  const geometry = series?.analysis?.sagittal_geometry;
  const lineY =
    located && geometry ? ((located.sagittal_fraction * (geometry.height - 1) + 0.5) / geometry.height) * 100 : null;
  const pixelAspect =
    series && series.pixel_spacing_row && series.pixel_spacing_col
      ? `${series.columns * series.pixel_spacing_col} / ${series.rows * series.pixel_spacing_row}`
      : undefined;

  if (total === 0 && !series && !error) {
    return (
      <div className="card empty">
        <h2>No series yet</h2>
        <p>Index a DICOM directory, upload a folder or submit a CSV on the <a href="#import">Import &amp; jobs</a> tab.</p>
      </div>
    );
  }

  return (
    <section className="viewer">
      <ErrorBox>{error}</ErrorBox>
      <div className="card toolbar">
        <label className="field grow">
          <span>Series</span>
          <select value={seriesId ?? ""} onChange={(e) => onSelectSeries(Number(e.target.value))}>
            {seriesId === null && <option value="" disabled>Choose a series</option>}
            {seriesId !== null && !seriesList.some((s) => s.id === seriesId) && (
              <option value={seriesId}>#{seriesId} · {series?.series_description ?? "Selected series"}</option>
            )}
            {seriesList.map((s) => (
              <option key={s.id} value={s.id}>
                #{s.id} · {s.study_description ?? "—"} · {s.series_description ?? "—"} ({s.num_slices} slices)
              </option>
            ))}
          </select>
          {total !== null && total > 50 && <span>
            <button type="button" disabled={listOffset === 0} onClick={() => setListOffset(Math.max(0, listOffset - 50))}>Previous series</button>
            {" "}{listOffset + 1}–{Math.min(listOffset + 50, total)} of {total}{" "}
            <button type="button" disabled={listOffset + 50 >= total} onClick={() => setListOffset(listOffset + 50)}>More series</button>
          </span>}
        </label>

        <form className="locate" onSubmit={submit}>
          <div className="segmented" role="radiogroup" aria-label="Input type">
            <button type="button" className={mode === "mm" ? "on" : ""} onClick={() => switchMode("mm")}>
              Location (mm)
            </button>
            <button type="button" className={mode === "index" ? "on" : ""} onClick={() => switchMode("index")}>
              Slice index
            </button>
          </div>
          <input
            type="number"
            step={mode === "mm" ? "any" : 1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              series
                ? mode === "mm"
                  ? `${Number(series.z_min.toFixed(1))} … ${Number(series.z_max.toFixed(1))}`
                  : `0 … ${series.num_slices - 1}`
                : ""
            }
            aria-label={mode === "mm" ? "Location in millimetres" : "Slice index"}
          />
          <button type="submit" className="primary" disabled={!series}>
            Show
          </button>
        </form>

        <div className="windowing">
          <label className="field">
            <span>Window preset</span>
            <select
              value={preset}
              onChange={(e) => {
                const p = PRESETS.find((x) => x.name === e.target.value);
                if (p) {
                  setWidth(p.width);
                  setLevel(p.level);
                }
              }}
            >
              {PRESETS.map((p) => (
                <option key={p.name}>{p.name}</option>
              ))}
              <option value="custom" disabled>
                Custom
              </option>
            </select>
          </label>
          <label className="field small">
            <span>Width</span>
            <input type="number" min={1} value={width} onChange={(e) => Number(e.target.value) > 0 && setWidth(Number(e.target.value))} />
          </label>
          <label className="field small">
            <span>Level</span>
            <input type="number" value={level} onChange={(e) => e.target.value !== "" && setLevel(Number(e.target.value))} />
          </label>
        </div>
      </div>

      {series && (
        <input
          className="slider"
          type="range"
          min={0}
          max={series.num_slices - 1}
          value={located?.index ?? 0}
          onChange={(e) => onSlider(Number(e.target.value))}
          aria-label="Slice"
        />
      )}

      <div className="status-line">
        {locateError ? (
          <span className="error-text">{locateError}</span>
        ) : located ? (
          <>
            Slice index <b>{located.index}</b> (0–{located.num_slices - 1}) · z = <b>{mm(located.z)}</b>
            {located.instance_number !== null && <> · instance #{located.instance_number}</>}
            {located.requested_z !== null && (
              <>
                {" "}
                · closest to {mm(located.requested_z)} ({mm(located.distance_mm)} away)
              </>
            )}
            {located.outside_volume && <span className="warn-text"> · outside the scanned range</span>}
          </>
        ) : (
          <span className="muted">Loading…</span>
        )}
      </div>

      {series && located && (
        <div className="panels">
          <figure className="viewport">
            <figcaption>Axial</figcaption>
            <div className="image-box">
              {axialFailed === axialSrc ? (
                <p className="error-text">This slice could not be displayed (unreadable or missing file).</p>
              ) : (
                <img
                  src={axialSrc!}
                  alt={`Axial slice ${located.index}`}
                  style={{ aspectRatio: pixelAspect }}
                  onError={() => setAxialFailed(axialSrc)}
                />
              )}
              <Overlay
                corners={[
                  [series.patient_id ?? "", series.study_description ?? ""],
                  [series.series_description ?? "", series.modality ?? ""],
                  [`W ${windowing.width}  L ${windowing.level}`, preset === "custom" ? "" : preset],
                  [`Slice ${located.index} (0–${located.num_slices - 1})`, `z ${mm(located.z)}`],
                ]}
                markers={["A", "L", "P", "R"]}
              />
            </div>
          </figure>
          <figure className="viewport">
            <figcaption>
              Sagittal, mid-volume <StatusBadge status={series.status} />
            </figcaption>
            {series.status === "ready" && series.analysis ? (
              <div className="image-box">
                <div className="overlay-wrap">
                  <img
                    src={api.sagittalUrl(series.id, windowing.width, windowing.level, series.analysis.created_at)}
                    alt="Sagittal view through the middle of the volume"
                  />
                  {lineY !== null && (
                    <div className="slice-line" style={{ top: `${lineY}%` }}>
                      <span>{mm(located.z, 1)}</span>
                    </div>
                  )}
                </div>
                <Overlay corners={[null, null, [`W ${windowing.width}  L ${windowing.level}`], null]} markers={["S", "P", "I", "A"]} />
              </div>
            ) : (
              <div className="placeholder">
                <AnalysisPanel series={series} />
              </div>
            )}
          </figure>
        </div>
      )}

      {series && (
        <div className="info-grid">
          <div className="card">
            <h3>Series metadata</h3>
            <MetadataTable series={series} />
          </div>
          <div className="card">
            <h3>Analysis &amp; QC</h3>
            <AnalysisPanel series={series} />
          </div>
        </div>
      )}
    </section>
  );
}
