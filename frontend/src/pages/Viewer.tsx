import {
  Alert,
  Button,
  Center,
  DataList,
  Grid,
  Group,
  NumberInput,
  SegmentedControl,
  Select,
  SimpleGrid,
  Slider,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { type SubmitEvent, type ReactNode, useState } from "react";
import { type Series, type SliceTarget, api, axialUrl, sagittalUrl } from "../api";

const PRESETS = [
  { label: "Soft tissue", width: 400, level: 40 },
  { label: "Lung", width: 1500, level: -600 },
  { label: "Bone", width: 1800, level: 400 },
  { label: "Brain", width: 80, level: 40 },
];

const mm = (value: number | null) => (value === null ? "Not in header" : `${Number(value.toFixed(2))} mm`);

interface Props {
  seriesId: number | null;
  onSelect: (id: number) => void;
  onGoToJobs: () => void;
}

export function Viewer({ seriesId, onSelect, onGoToJobs }: Props) {
  const list = useQuery({ queryKey: ["series", "all"], queryFn: () => api.searchSeries({}) });
  const id = seriesId ?? list.data?.[0]?.id ?? null;
  const series = useQuery({ queryKey: ["series", id], queryFn: () => api.getSeries(id!), enabled: id !== null });

  if (list.data?.length === 0) {
    return (
      <Center h="60vh">
        <Stack align="center" gap="xs">
          <Title order={2} fz="h3">No series yet</Title>
          <Text c="dimmed">Index a DICOM folder or a CSV of folders, then come back here.</Text>
          <Button mt="sm" onClick={onGoToJobs}>Go to jobs</Button>
        </Stack>
      </Center>
    );
  }

  return (
    <Stack gap="lg">
      <Select
        aria-label="Series"
        placeholder="Choose a series"
        searchable
        maw={720}
        value={id === null ? null : String(id)}
        onChange={(value) => value && onSelect(Number(value))}
        data={(list.data ?? []).map((s) => ({
          value: String(s.id),
          label: `${s.study_description ?? "No study description"}: ${s.series_description ?? "no series description"} (${s.num_slices} slices)`,
        }))}
      />
      {(list.error ?? series.error) && <Alert color="red">{(list.error ?? series.error)!.message}</Alert>}
      {/* The key resets the slice and window when another series (or a re-indexed one) is shown. */}
      {series.data && (series.data.analysis_available
        ? <SeriesView key={`${series.data.id}-${series.data.updated_at}`} series={series.data} />
        : <Stack>
            <Alert color="yellow">Metadata is indexed, but analysis is not available. Check Jobs for progress or the failure reason.</Alert>
            <Button variant="light" onClick={onGoToJobs}>Go to jobs</Button>
            <Metadata series={series.data} />
          </Stack>)}
    </Stack>
  );
}

function SeriesView({ series }: { series: Series }) {
  const [mode, setMode] = useState<"mm" | "index">("mm");
  const [input, setInput] = useState<string | number>("");
  const [target, setTarget] = useState<SliceTarget>({ index: Math.floor(series.num_slices / 2) });
  const located = useQuery({
    queryKey: ["locate", series.id, target],
    queryFn: () => api.locate(series.id, target),
    placeholderData: keepPreviousData, // keep showing the last slice while the next one is found
  });
  const slice = located.data;

  const [windowing, setWindowing] = useState({ width: 400, level: 40 });
  const [shown] = useDebouncedValue(windowing, 250); // one image request after typing stops, not one per keystroke
  const preset = PRESETS.find((p) => p.width === windowing.width && p.level === windowing.level)?.label ?? "Custom";

  function show(event: SubmitEvent) {
    event.preventDefault();
    if (input !== "") setTarget(mode === "mm" ? { z: Number(input) } : { index: Number(input) });
  }

  // Sagittal image rows run from the highest z (top) to the lowest; the line goes through the middle of the slice's row.
  const lineTop = slice ? ((slice.num_slices - 1 - slice.index + 0.5) / slice.num_slices) * 100 : null;

  return (
    <Grid gap="lg">
      <Grid.Col span={{ base: 12, lg: 8 }}>
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          <Panel title="Axial">
            {slice && (
              <>
                <div className="image">
                  <img src={axialUrl(series, slice.index, shown.width, shown.level)} alt={`Axial slice ${slice.index}`} />
                </div>
                <div className="corner top-left">
                  {series.patient_id}
                  <br />
                  {series.study_description}
                </div>
                <div className="corner bottom-left">
                  W {shown.width} L {shown.level}
                </div>
                <div className="corner bottom-right">
                  Slice {slice.index}
                  <br />z {mm(slice.z)}
                </div>
              </>
            )}
          </Panel>
          <Panel title="Sagittal, through the middle of the volume">
            <div className="image">
              <img src={sagittalUrl(series, shown.width, shown.level)} alt="Sagittal view with the chosen slice marked" />
              {slice && lineTop !== null && (
                <div className="slice-line" style={{ top: `${lineTop}%` }}>
                  <span>{mm(slice.z)}</span>
                </div>
              )}
            </div>
          </Panel>
        </SimpleGrid>
      </Grid.Col>

      <Grid.Col span={{ base: 12, lg: 4 }}>
        <Stack gap="xl">
          <Section title="Location">
            <SegmentedControl
              fullWidth
              value={mode}
              onChange={(value) => {
                setMode(value as "mm" | "index");
                setInput("");
              }}
              data={[
                { value: "mm", label: "Millimetres (z)" },
                { value: "index", label: "Slice index" },
              ]}
            />
            <form onSubmit={show}>
              <Group gap="xs" align="flex-start">
                <NumberInput
                  flex={1}
                  aria-label={mode === "mm" ? "Location in millimetres" : "Slice index"}
                  placeholder={
                    mode === "mm"
                      ? `${Number(series.z_min.toFixed(1))} to ${Number(series.z_max.toFixed(1))}`
                      : `0 to ${series.num_slices - 1}`
                  }
                  allowDecimal={mode === "mm"}
                  value={input}
                  onChange={setInput}
                />
                <Button type="submit">Show</Button>
              </Group>
            </form>
            <Slider
              color="yellow"
              min={0}
              max={series.num_slices - 1}
              value={"index" in target ? target.index : (slice?.index ?? 0)}
              onChange={(index) => setTarget({ index })}
              label={(index) => `Slice ${index}`}
              aria-label="Slice"
            />
            <SliceStatus target={target} error={located.error} slice={slice} series={series} />
          </Section>

          <Section title="Window">
            <SegmentedControl
              fullWidth
              size="xs"
              value={preset}
              onChange={(label) => {
                const p = PRESETS.find((x) => x.label === label);
                if (p) setWindowing({ width: p.width, level: p.level });
              }}
              data={[...PRESETS.map((p) => p.label), { label: "Custom", value: "Custom", disabled: true }]}
            />
            <Group grow>
              <NumberInput
                label="Width"
                min={1}
                value={windowing.width}
                onChange={(v) => typeof v === "number" && v > 0 && setWindowing((w) => ({ ...w, width: v }))}
              />
              <NumberInput
                label="Level"
                value={windowing.level}
                onChange={(v) => typeof v === "number" && setWindowing((w) => ({ ...w, level: v }))}
              />
            </Group>
          </Section>

          <Section title="Series details">
            <Metadata series={series} />
          </Section>
        </Stack>
      </Grid.Col>
    </Grid>
  );
}

function SliceStatus({ target, error, slice, series }: {
  target: SliceTarget;
  error: Error | null;
  slice: { index: number; z: number; distance_mm: number; outside: boolean } | undefined;
  series: Series;
}) {
  if (error) return <Text c="red.4" size="sm">{error.message}</Text>;
  if (!slice) return null;
  if (slice.outside) {
    return (
      <Text c="yellow.5" size="sm">
        That location is outside the scan ({mm(series.z_min)} to {mm(series.z_max)}). Showing the nearest slice.
      </Text>
    );
  }
  return (
    <Text c="dimmed" size="sm">
      Slice {slice.index} (0 to {series.num_slices - 1}) at z {mm(slice.z)}
      {"z" in target && `, ${mm(slice.distance_mm)} from the location you entered`}
    </Text>
  );
}

function Metadata({ series }: { series: Series }) {
  const tag = series.spacing_between_slices;
  const measured = series.slice_spacing;
  // Many CT series leave this tag out, and some write a value that the slice positions contradict.
  const spacing =
    tag === null
      ? `${mm(measured)} (measured; not in header)`
      : Math.abs(tag - measured) > 0.01
        ? `${mm(tag)} in header, ${mm(measured)} measured`
        : mm(tag);
  const rows: [string, ReactNode][] = [
    ["Patient ID", series.patient_id ?? "None"],
    ["Study", series.study_description ?? "None"],
    ["Series", series.series_description ?? "None"],
    ["Modality", series.modality ?? "None"],
    ["Pixel spacing", series.pixel_spacing ? `${series.pixel_spacing.map((v) => Number(v.toFixed(3))).join(" × ")} mm` : "Not in header"],
    ["Slice thickness", mm(series.slice_thickness)],
    ["Spacing between slices", spacing],
    ["Number of slices", series.num_slices],
    ["Rows", series.rows],
    ["Columns", series.columns],
    ["z range", `${mm(series.z_min)} to ${mm(series.z_max)}`],
  ];
  return (
    <DataList withDivider size="sm" labelWidth={170}>
      {rows.map(([label, value]) => (
        <DataList.Item key={label}>
          <DataList.ItemLabel>{label}</DataList.ItemLabel>
          <DataList.ItemValue style={{ overflowWrap: "anywhere" }}>{value}</DataList.ItemValue>
        </DataList.Item>
      ))}
    </DataList>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Stack gap={6}>
      <Text size="sm" fw={500} c="dimmed">{title}</Text>
      <div className="frame">{children}</div>
    </Stack>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Stack gap="sm">
      <Title order={2} fz="md">{title}</Title>
      {children}
    </Stack>
  );
}
