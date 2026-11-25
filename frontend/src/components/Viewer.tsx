import { type FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useDebounced, usePolling } from "../hooks";
import type { Located, Series, SeriesDetail } from "../types";
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

interface Props {
  seriesId: number | null;
  onSelectSeries: (id: number) => void;
}

export function Viewer({ seriesId, onSelectSeries }: Props) {
  const [seriesList, setSeriesList] = useState<Series[]>([]);
  const [series, setSeries] = useState<SeriesDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [mode, setMode] = useState<Mode>("mm");
  const [input, setInput] = useState("");
  const [located, setLocated] = useState<Located | null>(null);
  const [locateError, setLocateError] = useState<string | null>(null);
  const requestId = useRef(0);

  const [width, setWidth] = useState(400);
  const [level, setLevel] = useState(40);
  const windowing = useDebounced({ width, level }, 200);

  useEffect(() => {
    api
      .searchSeries({})
      .then((r) => {
        setSeriesList(r.items);
        if (seriesId === null && r.items.length > 0) onSelectSeries(r.items[0].id);
      })
      .catch((e) => setError(e.message));
    // Load the list once per visit to the tab.
  }, []);

  // Load the series; keep polling while its analysis (sagittal view) is still pending.
  const analysisPending = series !== null && series.id === seriesId && series.status === "indexed";
  usePolling(
    async () => {
      if (seriesId === null) return;
      try {
        setSeries(await api.getSeries(seriesId));
        setError(null);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    analysisPending ? 2000 : null,
    [seriesId],
  );

  async function goTo(by: { z: number } | { index: number }) {
    if (seriesId === null) return;
    const id = ++requestId.current; // drop answers that arrive after a newer request
    try {
      const result = await api.locate(seriesId, by);
      if (id !== requestId.current) return;
      setLocated(result);
      setLocateError(null);
    } catch (e) {
      if (id === requestId.current) setLocateError((e as Error).message);
    }
  }

  // Open each new series at its middle slice.
  const loadedId = series?.id;
  useEffect(() => {
    if (!series) return;
    const middle = Math.floor(series.num_slices / 2);
    setLocated(null);
    setInput(mode === "index" ? String(middle) : "");
    void goTo({ index: middle });
    // Only when a different series is loaded, not on every refresh of the same one.
  }, [loadedId]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = Number(input);
    if (input.trim() === "" || Number.isNaN(value)) {
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
    series && located ? api.axialUrl(series.id, located.index, windowing.width, windowing.level) : null;

  const preset = PRESETS.find((p) => p.width === width && p.level === level)?.name ?? "custom";
  const geometry = series?.analysis?.sagittal_geometry;
  const lineY =
    located && geometry ? ((located.sagittal_fraction * (geometry.height - 1) + 0.5) / geometry.height) * 100 : null;
  const pixelAspect =
    series && series.pixel_spacing_row && series.pixel_spacing_col
      ? `${series.columns * series.pixel_spacing_col} / ${series.rows * series.pixel_spacing_row}`
      : undefined;

  if (seriesList.length === 0 && !error) {
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
            {seriesList.map((s) => (
              <option key={s.id} value={s.id}>
                #{s.id} · {s.study_description ?? "—"} · {s.series_description ?? "—"} ({s.num_slices} slices)
              </option>
            ))}
          </select>
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
          <button type="submit" className="primary">
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
          <figure className="card">
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
            </div>
          </figure>
          <figure className="card">
            <figcaption>
              Sagittal (mid-volume) <StatusBadge status={series.status} />
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
