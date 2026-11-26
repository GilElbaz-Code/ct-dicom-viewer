import {
  Alert,
  Badge,
  Button,
  FileInput,
  Group,
  Paper,
  Pill,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";
import { type JobStatus, api, nextPage } from "../api";

const STATUS: Record<JobStatus, { label: string; color: string }> = {
  queued: { label: "Queued", color: "gray" },
  running: { label: "Running", color: "blue" },
  done: { label: "Done", color: "teal" },
  failed: { label: "Failed", color: "red" },
};

/** Jobs are stored in the database, so this page shows the same list after closing and reopening the browser. */
export function Jobs({ onOpen }: { onOpen: (id: number) => void }) {
  const queryClient = useQueryClient();
  const [batchId, setBatchId] = useState<string | null>(null);
  const [path, setPath] = useState("");
  const [csv, setCsv] = useState<File | null>(null);

  const directories = useQuery({ queryKey: ["directories"], queryFn: api.directories });
  const jobs = useInfiniteQuery({
    queryKey: ["jobs", batchId],
    queryFn: ({ pageParam }) => api.jobs(batchId, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: nextPage,
    // Poll only while something is still waiting or running.
    refetchInterval: (query) =>
      query.state.data?.pages.flat().some((job) => job.status === "queued" || job.status === "running") ? 2000 : false,
  });
  const jobList = jobs.data?.pages.flat();

  const refreshJobs = () => queryClient.invalidateQueries({ queryKey: ["jobs"] });
  const indexFolder = useMutation({
    mutationFn: api.indexFolder,
    onSuccess: () => {
      setBatchId(null);
      setPath("");
      refreshJobs();
    },
  });
  const indexCsv = useMutation({
    mutationFn: api.indexCsv,
    onSuccess: (created) => {
      setBatchId(created[0]?.batch_id ?? null);
      setCsv(null);
      refreshJobs();
    },
  });
  const error = indexFolder.error ?? indexCsv.error ?? jobs.error ?? directories.error;

  function submitPath(event: SubmitEvent) {
    event.preventDefault();
    if (path.trim()) indexFolder.mutate(path.trim());
  }

  return (
    <Stack gap="lg">
      {error && <Alert color="red">{error.message}</Alert>}

      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
        <Paper withBorder p="md">
          <Title order={2} fz="md">Index a folder</Title>
          <Text size="sm" c="dimmed" mt={4}>
            Each folder holds one DICOM series. Indexing reads the headers, saves the metadata and builds the
            sagittal view.
          </Text>
          <Stack gap={0} my="sm">
            {directories.data?.map((name) => (
              <Group key={name} justify="space-between" py={6} style={{ borderBottom: "1px solid var(--mantine-color-dark-4)" }}>
                <Text size="sm">{name}</Text>
                <Button size="compact-sm" variant="light" disabled={indexFolder.isPending} onClick={() => indexFolder.mutate(name)}>
                  Index
                </Button>
              </Group>
            ))}
            {directories.data?.length === 0 && (
              <Text size="sm">No folders in the data directory yet. Copy one folder per series into data/.</Text>
            )}
          </Stack>
          <form onSubmit={submitPath}>
            <Group gap="xs" align="flex-end">
              <TextInput
                flex={1}
                label="Or type a path inside the data directory"
                placeholder="study_1/series_2"
                value={path}
                onChange={(event) => setPath(event.currentTarget.value)}
              />
              <Button type="submit" variant="default" loading={indexFolder.isPending}>
                Index
              </Button>
            </Group>
          </form>
        </Paper>

        <Paper withBorder p="md">
          <Title order={2} fz="md">Index from a CSV</Title>
          <Text size="sm" c="dimmed" mt={4}>
            One folder path per row, relative to the data directory. A first row reading "path" is skipped. Every row
            becomes its own job, so one bad row does not stop the others.
          </Text>
          <Group gap="xs" align="flex-end" mt="sm">
            <FileInput
              flex={1}
              label="CSV file"
              placeholder="Choose a .csv file"
              accept=".csv,text/csv"
              clearable
              value={csv}
              onChange={setCsv}
            />
            <Button disabled={!csv} loading={indexCsv.isPending} onClick={() => csv && indexCsv.mutate(csv)}>
              Index all rows
            </Button>
          </Group>
        </Paper>
      </SimpleGrid>

      <Paper withBorder p="md">
        <Group justify="space-between" mb="sm">
          <Title order={2} fz="md">Jobs</Title>
          {batchId && (
            <Pill withRemoveButton onRemove={() => setBatchId(null)} removeButtonProps={{ "aria-label": "Show all jobs" }}>
              Only CSV batch {batchId.slice(0, 8)}
            </Pill>
          )}
        </Group>
        <Table.ScrollContainer minWidth={760}>
          <Table tabularNums verticalSpacing="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Job</Table.Th>
                <Table.Th>Folder</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Result</Table.Th>
                <Table.Th>Submitted</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {jobList?.map((job) => (
                <Table.Tr key={job.id}>
                  <Table.Td>{job.id}</Table.Td>
                  <Table.Td>
                    {job.path}
                    {job.batch_id && !batchId && (
                      <Button size="compact-xs" variant="subtle" ml="xs" onClick={() => setBatchId(job.batch_id)}>
                        CSV batch
                      </Button>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <Badge variant="light" tt="none" color={STATUS[job.status].color}>
                      {STATUS[job.status].label}
                    </Badge>
                    <Text size="xs" c="dimmed">{job.stage} · attempt {job.attempt_count}</Text>
                  </Table.Td>
                  <Table.Td maw={420}>
                    {job.status === "failed" && <Text size="sm" c="red.4">{job.error}</Text>}
                    {job.status === "done" && job.series_id !== null && (
                      <Button size="compact-sm" variant="subtle" onClick={() => onOpen(job.series_id!)}>
                        Open in viewer
                      </Button>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" c="dimmed">{new Date(job.created_at).toLocaleString()}</Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {jobList?.length === 0 && <Text size="sm" mt="sm">No jobs yet. Index a folder or a CSV above.</Text>}
        {jobs.hasNextPage && (
          <Button variant="default" mt="sm" loading={jobs.isFetchingNextPage} onClick={() => jobs.fetchNextPage()}>
            Load older jobs
          </Button>
        )}
      </Paper>
    </Stack>
  );
}
