import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../src/api";
import { Viewer } from "../src/components/Viewer";
import { SeriesBrowser } from "../src/components/SeriesBrowser";
import { JobsTable } from "../src/components/JobsTable";
import { deferred, series } from "./fixtures";
import type { Job, SeriesDetail } from "../src/types";

afterEach(cleanup);

it("keeps the new series when an old response arrives late (component; mocked API)", async () => {
  const old = deferred<SeriesDetail>();
  vi.spyOn(api, "searchSeries").mockResolvedValue({ items: [series(1), series(2)], total: 2 });
  vi.spyOn(api, "getSeries").mockImplementation((id) => id === 1 ? old.promise : Promise.resolve(series(2)));
  const locate = vi.spyOn(api, "locate");
  const select = vi.fn();
  const { rerender } = render(<Viewer seriesId={1} onSelectSeries={select} />);
  rerender(<Viewer seriesId={2} onSelectSeries={select} />);
  await waitFor(() => expect(screen.getByAltText("Axial slice 1").getAttribute("src")).toContain("/series/2/"));
  await act(async () => { old.resolve(series(1)); await old.promise; });
  expect(screen.getByAltText("Axial slice 1").getAttribute("src")).toContain("/series/2/");
  fireEvent.change(screen.getByLabelText("Slice"), { target: { value: "2" } });
  expect(screen.getByAltText("Axial slice 2").getAttribute("src")).toContain("/series/2/axial/2");
  expect(locate).not.toHaveBeenCalled();
});

it("reaches later search pages with the active filters", async () => {
  const search = vi.spyOn(api, "searchSeries").mockResolvedValue({ items: [series()], total: 501 });
  render(<SeriesBrowser onOpen={vi.fn()} />);
  await waitFor(() => expect(screen.getByText("study-1")).toBeTruthy());
  fireEvent.click(screen.getByText("Next series"));
  await waitFor(() => expect(search.mock.calls.at(-1)?.[0].offset).toBe(50));
});

it("reaches older jobs instead of silently truncating the table", async () => {
  const jobs = Array.from({ length: 51 }, (_, i) => ({
    id: i + 1, kind: "ingest", status: "succeeded", source_path: "example", series_id: 1,
    batch_id: null, batch_row: null, parent_job_id: null, submitted_by: "anonymous", attempts: 1, max_attempts: 3,
    error_code: null, error_message: null, result: {}, created_at: "2026-01-01T00:00:00Z", started_at: null, finished_at: null,
  })) as Job[];
  const list = vi.spyOn(api, "listJobs").mockResolvedValue(jobs);
  render(<JobsTable refreshKey={0} batchId={null} onClearBatch={vi.fn()} onOpenSeries={vi.fn()} />);
  await waitFor(() => expect((screen.getByText("Next jobs") as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText("Next jobs"));
  // The next page starts below the last job shown (id 50), not at an offset that new jobs would shift.
  await waitFor(() => expect(list.mock.calls.at(-1)?.[0].before_id).toBe(50));
});
