import { useEffect, useState } from "react";
import { ImportPanel } from "./components/ImportPanel";
import { SeriesBrowser } from "./components/SeriesBrowser";
import { Viewer } from "./components/Viewer";

type Tab = "viewer" | "series" | "import";
const TABS: [Tab, string][] = [
  ["viewer", "Viewer"],
  ["series", "Series"],
  ["import", "Import & jobs"],
];

/** State lives in the URL hash (#viewer?series=3), so links and reloads land on the same view. */
function readHash(): { tab: Tab; seriesId: number | null } {
  const [name, qs] = window.location.hash.replace(/^#/, "").split("?");
  const tab = (TABS.find(([t]) => t === name)?.[0] ?? "viewer") as Tab;
  const id = new URLSearchParams(qs).get("series");
  return { tab, seriesId: id ? Number(id) : null };
}

export function App() {
  const [{ tab, seriesId }, setRoute] = useState(readHash);

  useEffect(() => {
    const onHash = () => setRoute(readHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = (next: Tab, id: number | null = seriesId) => {
    window.location.hash = id !== null && next === "viewer" ? `${next}?series=${id}` : next;
  };
  const openSeries = (id: number) => go("viewer", id);

  return (
    <div className="app">
      <header>
        <div className="brand">
          {/* gantry ring with an axial plane through it */}
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2" />
            <circle cx="12" cy="12" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.55" />
            <line x1="1.5" y1="12" x2="22.5" y2="12" stroke="var(--line)" strokeWidth="1.5" />
          </svg>
          <h1>CT DICOM Viewer</h1>
        </div>
        <nav>
          {TABS.map(([t, label]) => (
            <button
              key={t}
              className={t === tab ? "tab on" : "tab"}
              aria-current={t === tab ? "page" : undefined}
              onClick={() => go(t)}
            >
              {label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {tab === "viewer" && (
          <Viewer seriesId={seriesId} onSelectSeries={openSeries} />
        )}
        {tab === "series" && <SeriesBrowser onOpen={openSeries} />}
        {tab === "import" && <ImportPanel onOpenSeries={openSeries} />}
      </main>
    </div>
  );
}
