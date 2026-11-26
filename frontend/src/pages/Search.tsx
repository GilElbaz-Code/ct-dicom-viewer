import { Alert, Button, Group, NumberInput, Paper, Stack, Table, Text, TextInput } from "@mantine/core";
import { useInfiniteQuery } from "@tanstack/react-query";
import { type SubmitEvent, useState } from "react";
import { api, nextPage } from "../api";

type Filters = { max_slice_thickness?: number; description?: string };

/** Queries across every indexed study, e.g. all series thinner than 1.5 mm. */
export function Search({ onOpen }: { onOpen: (id: number) => void }) {
  const [thickness, setThickness] = useState<string | number>("");
  const [text, setText] = useState("");
  const [filters, setFilters] = useState<Filters>({});
  const result = useInfiniteQuery({
    queryKey: ["series", "search", filters],
    queryFn: ({ pageParam }) => api.searchSeries(filters, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: nextPage,
  });
  const rows = result.data?.pages.flat();

  function search(event: SubmitEvent) {
    event.preventDefault();
    setFilters({
      max_slice_thickness: thickness === "" || Number(thickness) <= 0 ? undefined : Number(thickness),
      description: text.trim() || undefined,
    });
  }

  return (
    <Stack gap="lg">
      <Paper withBorder p="md">
        <form onSubmit={search}>
          <Group align="flex-end">
            <NumberInput
              label="Slice thickness under (mm)"
              placeholder="Any"
              min={0}
              step={0.5}
              w={220}
              value={thickness}
              onChange={setThickness}
            />
            <TextInput
              label="Description contains"
              placeholder="Study or series description, e.g. chest"
              flex={1}
              miw={240}
              value={text}
              onChange={(event) => setText(event.currentTarget.value)}
            />
            <Button type="submit">Search</Button>
          </Group>
        </form>
      </Paper>

      {result.error && <Alert color="red">{result.error.message}</Alert>}

      <Paper withBorder p="md">
        <Text size="sm" c="dimmed" mb="sm">
          {rows ? `${rows.length}${result.hasNextPage ? " most recent" : ""} series` : "Searching…"}
        </Text>
        <Table.ScrollContainer minWidth={760}>
          <Table highlightOnHover tabularNums verticalSpacing="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Patient ID</Table.Th>
                <Table.Th>Study</Table.Th>
                <Table.Th>Series</Table.Th>
                <Table.Th>Modality</Table.Th>
                <Table.Th>Thickness</Table.Th>
                <Table.Th>Slices</Table.Th>
                <Table.Th>Size</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows?.map((s) => (
                <Table.Tr key={s.id}>
                  <Table.Td>{s.patient_id}</Table.Td>
                  <Table.Td>{s.study_description}</Table.Td>
                  <Table.Td>{s.series_description}</Table.Td>
                  <Table.Td>{s.modality}</Table.Td>
                  <Table.Td>{s.slice_thickness === null ? "" : `${s.slice_thickness} mm`}</Table.Td>
                  <Table.Td>{s.num_slices}</Table.Td>
                  <Table.Td>
                    {s.rows} × {s.columns}
                  </Table.Td>
                  <Table.Td>
                    <Button size="compact-sm" variant="subtle" onClick={() => onOpen(s.id)}>
                      Open
                    </Button>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {result.hasNextPage && (
          <Button variant="default" mt="sm" loading={result.isFetchingNextPage} onClick={() => result.fetchNextPage()}>
            Load older series
          </Button>
        )}
        {rows?.length === 0 && (
          <Text size="sm" mt="sm">
            No series match these filters. Clear them, or index more folders on the Jobs tab.
          </Text>
        )}
      </Paper>
    </Stack>
  );
}
