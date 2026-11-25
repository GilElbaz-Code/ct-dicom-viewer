import { describe, expect, it } from "vitest";
import { locateSlice } from "../src/location";
import { series } from "./fixtures";

describe("local slice lookup (unit; no HTTP or DICOM decoding)", () => {
  it.each([[-100, 0], [0.5, 0], [0.6, 1], [3, 1], [3.1, 2], [100, 2]])(
    "resolves z=%s to index=%s, including ties and gaps", (z, index) => {
      expect(locateSlice(series(), { z }).index).toBe(index);
    },
  );
  it("uses physical positions for the line and reports outside requests", () => {
    expect(locateSlice(series(), { index: 1 }).sagittal_fraction).toBe(0.8);
    expect(locateSlice(series(), { z: 100 })).toMatchObject({ z: 5, distance_mm: 95, outside_volume: true });
  });
  it.each([NaN, Infinity, -Infinity])("rejects nonfinite z=%s", (z) => {
    expect(() => locateSlice(series(), { z })).toThrow();
  });
  it.each([-1, 3, 0.5, NaN])("rejects invalid index=%s", (index) => {
    expect(() => locateSlice(series(), { index })).toThrow();
  });
});
