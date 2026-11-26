import { AppShell, Group, SegmentedControl, Title } from "@mantine/core";
import { useLocalStorage } from "@mantine/hooks";
import { Jobs } from "./pages/Jobs";
import { Search } from "./pages/Search";
import { Viewer } from "./pages/Viewer";

type Tab = "viewer" | "search" | "jobs";

/** Gantry ring with the amber slice line: the same amber marks the slice location in the viewer. */
function Logo() {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="var(--mantine-color-cyan-4)" strokeWidth="2" />
      <line x1="1" y1="12" x2="23" y2="12" stroke="var(--mantine-color-yellow-5)" strokeWidth="1.5" />
    </svg>
  );
}

export function App() {
  // Kept in localStorage, so a reload or a later visit opens the same tab and series.
  const [tab, setTab] = useLocalStorage<Tab>({ key: "ct-viewer-tab", defaultValue: "viewer" });
  const [seriesId, setSeriesId] = useLocalStorage<number | null>({ key: "ct-viewer-series", defaultValue: null });

  const openSeries = (id: number) => {
    setSeriesId(id);
    setTab("viewer");
  };

  return (
    <AppShell header={{ height: 60 }} padding="lg">
      <AppShell.Header px="lg">
        <Group h="100%" justify="space-between">
          <Group gap="sm">
            <Logo />
            <Title order={1} fz="lg">
              CT Viewer
            </Title>
          </Group>
          <SegmentedControl
            value={tab}
            onChange={(value) => setTab(value as Tab)}
            data={[
              { value: "viewer", label: "Viewer" },
              { value: "search", label: "Search" },
              { value: "jobs", label: "Jobs" },
            ]}
          />
        </Group>
      </AppShell.Header>
      <AppShell.Main>
        {tab === "viewer" && <Viewer seriesId={seriesId} onSelect={setSeriesId} onGoToJobs={() => setTab("jobs")} />}
        {tab === "search" && <Search onOpen={openSeries} />}
        {tab === "jobs" && <Jobs onOpen={openSeries} />}
      </AppShell.Main>
    </AppShell>
  );
}
