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
        <h1>CT DICOM Viewer</h1>
        <nav>
          {TABS.map(([t, label]) => (
            <button key={t} className={t === tab ? "tab on" : "tab"} onClick={() => go(t)}>
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
