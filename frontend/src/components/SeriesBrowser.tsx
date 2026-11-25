import { type FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import type { Series } from "../types";
import { ErrorBox, StatusBadge, mm } from "./common";

interface Filters {
  q: string;
  study_description: string;
  max_slice_thickness: string;
  modality: string;
}

const EMPTY: Filters = { q: "", study_description: "", max_slice_thickness: "", modality: "" };

/** Cross-study search over the metadata index. */
export function SeriesBrowser({ onOpen }: { onOpen: (id: number) => void }) {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [query, setQuery] = useState({ filters: EMPTY, offset: 0 });
  const [items, setItems] = useState<Series[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    async function search() {
    try {
      const r = await api.searchSeries({ ...query.filters, offset: query.offset }, controller.signal);
      if (controller.signal.aborted) return;
      setItems(r.items);
      setTotal(r.total);
      setError(null);
    } catch (e) {
      if (!controller.signal.aborted) setError((e as Error).message);
    }
    }
    void search();
    return () => controller.abort();
  }, [query]);

  const set = (key: keyof Filters) => (e: { target: { value: string } }) =>
    setFilters({ ...filters, [key]: e.target.value });

  function submit(event: FormEvent) {
    event.preventDefault();
    setQuery({ filters, offset: 0 });
  }

  function example(f: Partial<Filters>) {
    const next = { ...EMPTY, ...f };
    setFilters(next);
    setQuery({ filters: next, offset: 0 });
  }

  return (
    <section>
      <form className="card filters" onSubmit={submit}>
        <label className="field grow">
          <span>Description contains</span>
          <input value={filters.q} onChange={set("q")} placeholder="study or series description" />
        </label>
        <label className="field grow">
          <span>Study description (exact)</span>
          <input value={filters.study_description} onChange={set("study_description")} />
        </label>
        <label className="field small">
          <span>Slice thickness &lt; (mm)</span>
          <input type="number" step="any" min={0} value={filters.max_slice_thickness} onChange={set("max_slice_thickness")} />
        </label>
        <label className="field small">
          <span>Modality</span>
          <input value={filters.modality} onChange={set("modality")} placeholder="CT" />
        </label>
        <button className="primary" type="submit">
          Search
        </button>
        <div className="examples">
          Examples:
          <button type="button" className="link" onClick={() => example({ max_slice_thickness: "1.5" })}>
            slice thickness under 1.5 mm
          </button>
          <button type="button" className="link" onClick={() => example({})}>
            all series
          </button>
        </div>
      </form>
      <ErrorBox>{error}</ErrorBox>
      <div className="card">
        <p className="muted">{total} series</p>
        <table className="grid">
          <thead>
            <tr>
              <th>#</th>
              <th>Patient</th>
              <th>Study</th>
              <th>Series</th>
              <th>Modality</th>
              <th>Thickness</th>
              <th>Slices</th>
              <th>Size</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id}>
                <td>{s.id}</td>
                <td className="mono">{s.patient_id ?? "—"}</td>
                <td>{s.study_description ?? "—"}</td>
                <td>{s.series_description ?? "—"}</td>
                <td>{s.modality ?? "—"}</td>
                <td>{mm(s.slice_thickness)}</td>
                <td>{s.num_slices}</td>
                <td>
                  {s.rows}×{s.columns}
                </td>
                <td>
                  <StatusBadge status={s.status} />
                </td>
                <td>
                  <button className="small" onClick={() => onOpen(s.id)}>
                    Open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row">
          <button disabled={query.offset === 0} onClick={() => setQuery({ ...query, offset: Math.max(0, query.offset - 50) })}>Previous series</button>
          <span>Page {query.offset / 50 + 1} · {total} series</span>
          <button disabled={query.offset + 50 >= total} onClick={() => setQuery({ ...query, offset: query.offset + 50 })}>Next series</button>
        </div>
      </div>
    </section>
  );
}
