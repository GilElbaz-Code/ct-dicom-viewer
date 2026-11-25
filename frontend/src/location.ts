import type { Located, SeriesDetail } from "./types";

export type SliceLocation = { z: number } | { index: number };

/** Resolve against actual positions, including gaps. O(log N) for z, O(1) for an index. */
export function locateSlice(series: SeriesDetail, by: SliceLocation): Located {
  const slices = series.slices;
  if (!slices.length) throw new Error("This series has no slices");
  let index: number;
  const requested = "z" in by ? by.z : null;
  if ("index" in by) {
    index = by.index;
    if (!Number.isInteger(index) || index < 0 || index >= slices.length) {
      throw new Error(`Slice index must be between 0 and ${slices.length - 1}`);
    }
  } else {
    if (!Number.isFinite(by.z)) throw new Error("Enter a finite location in mm");
    let lo = 0;
    let hi = slices.length;
    while (lo < hi) {
      const mid = Math.floor((lo + hi) / 2);
      if (slices[mid].z_position < by.z) lo = mid + 1;
      else hi = mid;
    }
    index = lo === 0 ? 0 : lo === slices.length ? lo - 1
      : slices[lo].z_position - by.z < by.z - slices[lo - 1].z_position ? lo : lo - 1;
  }
  const hit = slices[index];
  const halfStep = series.computed_slice_spacing / 2;
  const span = series.z_max - series.z_min;
  return {
    index, z: hit.z_position, instance_number: hit.instance_number, num_slices: slices.length,
    requested_z: requested, distance_mm: requested === null ? 0 : Math.abs(hit.z_position - requested),
    outside_volume: requested !== null && (requested < series.z_min - halfStep || requested > series.z_max + halfStep),
    sagittal_fraction: span ? (series.z_max - hit.z_position) / span : 0,
  };
}
