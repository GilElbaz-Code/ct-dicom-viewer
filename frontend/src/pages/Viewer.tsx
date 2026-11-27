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
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { type MouseEvent, type SubmitEvent, type ReactNode, useRef, useState } from "react";
import { type Series, type SliceTarget, WINDOW_LIMITS, api, axialUrl, imageError, isStale, sagittalUrl } from "../api";

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
  const queryClient = useQueryClient();
  // A location or image request found a newer version: reload the metadata. Its new updated_at changes the
  // SeriesView key, so the view remounts and every slice, image URL and pending answer of the old version is dropped.
  const reload = () => queryClient.invalidateQueries({ queryKey: ["series"] });

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

  // The newest page of series, plus the open one when it is older (opened from Search).
  const choices = [...(list.data ?? [])];
  if (series.data && !choices.some((s) => s.id === series.data.id)) choices.push(series.data);

  const picker = (
    <Select
      label="Series"
      placeholder="Choose a series"
      searchable
      w={360}
      value={id === null ? null : String(id)}
      onChange={(value) => value && onSelect(Number(value))}
      data={choices.map((s) => ({
        value: String(s.id),
        label: `${s.study_description ?? "No study description"}: ${s.series_description ?? "no series description"} (${s.num_slices} slices)`,
      }))}
    />
  );

  return (
    <Stack gap="md">
      {(list.error ?? series.error) && <Alert color="red">{(list.error ?? series.error)!.message}</Alert>}
      {/* The key resets the slice and window when another series (or a re-indexed one) is shown. */}
      {series.data?.analysis_available
        ? <SeriesView key={`${series.data.id}-${series.data.updated_at}`} series={series.data} picker={picker} onStale={reload} />
        : <Stack maw={520}>
            {picker}
            {series.data && <>
              <Alert color="yellow">Metadata is indexed, but analysis is not available. Check Jobs for progress or the failure reason.</Alert>
              <Button variant="light" onClick={onGoToJobs}>Go to jobs</Button>
              <Metadata series={series.data} />
            </>}
          </Stack>}
    </Stack>
  );
}

function SeriesView({ series, picker, onStale }: { series: Series; picker: ReactNode; onStale: () => void }) {
  // One reload per shown version, so a conflict that persists shows its message instead of reloading in a loop.
  const reloaded = useRef(false);
  const stale = (error: unknown) => {
    if (isStale(error) && !reloaded.current) {
      reloaded.current = true;
      onStale();
    }
  };
  const [mode, setMode] = useState<"mm" | "index">("mm");
  const [input, setInput] = useState<string | number>("");
  const [target, setTarget] = useState<SliceTarget>({ index: Math.floor(series.num_slices / 2) });
  const located = useQuery({
    queryKey: ["locate", series.id, series.updated_at, target],
    queryFn: () =>
      api.locate(series, target).catch((error: unknown) => {
        stale(error);
        throw error;
      }),
    placeholderData: keepPreviousData, // keep showing the last slice while the next one is found
  });
  const slice = located.data;

  // The input follows the shown slice (slider, sagittal click, Show), except a z the user entered stays as typed.
  const [synced, setSynced] = useState(slice);
  if (slice !== synced) {
    setSynced(slice);
    if (slice && (mode === "index" || "index" in target)) setInput(mode === "index" ? slice.index : Number(slice.z.toFixed(2)));
  }

  const [windowing, setWindowing] = useState({ width: 400, level: 40 });
  const [shown] = useDebouncedValue(windowing, 250); // one image request after typing stops, not one per keystroke
  const preset = PRESETS.find((p) => p.width === windowing.width && p.level === windowing.level)?.label ?? ""; // "": typed values match no preset, so none is highlighted

  // Switching units keeps the location: the typed value is converted, or the shown slice is used when nothing is typed.
  function switchUnit(next: "mm" | "index") {
    const step = series.num_slices > 1 ? (series.z_max - series.z_min) / (series.num_slices - 1) : 1; // spacing is validated uniform
    const toIndex = (z: number) => Math.min(series.num_slices - 1, Math.max(0, Math.round((z - series.z_min) / step)));
    const toZ = (index: number) => Number((series.z_min + index * step).toFixed(2));
    const typed = input === "" ? NaN : Number(input); // NaN also covers partial input such as "-"
    setMode(next);
    if (Number.isFinite(typed)) setInput(next === "index" ? toIndex(typed) : toZ(typed));
    else if (slice) setInput(next === "index" ? slice.index : Number(slice.z.toFixed(2)));
  }

  function show(event: SubmitEvent) {
    event.preventDefault();
    if (input !== "") setTarget(mode === "mm" ? { z: Number(input) } : { index: Number(input) });
  }

  // Sagittal image rows run from the highest z (top) to the lowest; the line goes through the middle of the slice's row.
  const lineTop = slice ? ((slice.num_slices - 1 - slice.index + 0.5) / slice.num_slices) * 100 : null;

  // The inverse of lineTop: the clicked image row becomes the slice index (the slider stays the keyboard path).
  function pickSlice(event: MouseEvent<HTMLDivElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    const row = Math.floor(((event.clientY - box.top) / box.height) * series.num_slices);
    setTarget({ index: Math.min(series.num_slices - 1, Math.max(0, series.num_slices - 1 - row)) });
  }

  // Every control sits in one toolbar row, so the images and the details column both fit the window height.
  return (
    <Stack gap="md">
      <Group gap="lg" align="flex-end">
        {picker}
        <form onSubmit={show}>
          <Group gap="xs" align="flex-end">
            <SegmentedControl
              aria-label="Location unit"
              value={mode}
              onChange={(value) => switchUnit(value as "mm" | "index")}
              data={[
                { value: "mm", label: "z (mm)" },
                { value: "index", label: "Index" },
              ]}
            />
            <NumberInput
              w={150}
              label={mode === "mm" ? "Location in mm" : "Slice index"}
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
        <Group gap="xs" align="flex-end">
          <SegmentedControl
            aria-label="Window preset"
            value={preset}
            onChange={(label) => {
              const p = PRESETS.find((x) => x.label === label);
              if (p) setWindowing({ width: p.width, level: p.level });
            }}
            data={PRESETS.map((p) => p.label)}
          />
          <NumberInput
            w={90}
            label="Width"
            {...WINDOW_LIMITS.width}
            clampBehavior="strict"
            value={windowing.width}
            onChange={(v) => typeof v === "number" && inRange(v, WINDOW_LIMITS.width) && setWindowing((w) => ({ ...w, width: v }))}
          />
          <NumberInput
            w={90}
            label="Level"
            {...WINDOW_LIMITS.level}
            clampBehavior="strict"
            value={windowing.level}
            onChange={(v) => typeof v === "number" && inRange(v, WINDOW_LIMITS.level) && setWindowing((w) => ({ ...w, level: v }))}
          />
        </Group>
      </Group>

      <Grid gap="lg">
        <Grid.Col span={{ base: 12, lg: 8 }}>
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          <Panel title="Axial">
            {slice && (
              <>
                <div className="image">
                  <ApiImage src={axialUrl(series, slice.index, shown.width, shown.level)} alt={`Axial slice ${slice.index}`} onFailure={stale} />
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
          <Panel title="Sagittal, mid-volume · click to pick a slice">
            <div className="image pickable" onClick={pickSlice}>
              <ApiImage src={sagittalUrl(series, shown.width, shown.level)} alt="Sagittal view with the chosen slice marked" onFailure={stale}>
                {slice && lineTop !== null && (
                  <div className="slice-line" style={{ top: `${lineTop}%` }}>
                    <span>{mm(slice.z)}</span>
                  </div>
                )}
              </ApiImage>
            </div>
          </Panel>
        </SimpleGrid>
      </Grid.Col>

      <Grid.Col span={{ base: 12, lg: 4 }}>
        <Stack gap="lg">
          <Section title="Slice">
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

          <Section title="Series details">
            <Metadata series={series} />
          </Section>
        </Stack>
      </Grid.Col>
    </Grid>
    </Stack>
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
    <DataList size="xs" labelWidth={150}>
      {rows.map(([label, value]) => (
        <DataList.Item key={label}>
          <DataList.ItemLabel>{label}</DataList.ItemLabel>
          <DataList.ItemValue style={{ overflowWrap: "anywhere" }}>{value}</DataList.ItemValue>
        </DataList.Item>
      ))}
    </DataList>
  );
}

const inRange = (value: number, { min, max }: { min: number; max: number }) => value >= min && value <= max;

/** An image from the API. If it fails, the API's reason replaces the browser's broken-image icon. */
function ApiImage({ src, alt, onFailure, children }: {
  src: string;
  alt: string;
  onFailure: (error: Error) => void; // e.g. a stale version: the viewer reloads the series
  children?: ReactNode;
}) {
  const [failure, setFailure] = useState<{ src: string; message: string } | null>(null);
  if (failure?.src === src) {
    return <Text c="red.4" size="sm" p="md">Image unavailable: {failure.message}</Text>;
  }
  return (
    <>
      <img
        src={src}
        alt={alt}
        onError={() =>
          imageError(src).then((error) => {
            onFailure(error);
            setFailure({ src, message: error.message });
          })
        }
      />
      {children}
    </>
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
