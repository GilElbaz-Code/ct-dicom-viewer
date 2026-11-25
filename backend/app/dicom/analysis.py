"""The analysis step: one streaming pass over the pixel data of a series.

It builds the mid-sagittal plane used by the viewer and a few quality-control
numbers. Slices are processed one at a time, so memory stays at one slice plus
one column per slice, whatever the size of the series (a 1 GB series would
need ~1.6 GB as a float32 volume).

In the full design this is where a real analysis (segmentation, radiomics, a
model on a GPU worker) would plug in; the job plumbing does not change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.dicom.imaging import SagittalGeometry, load_slice_hu, resample_sagittal
from app.errors import SeriesValidationError

HU_MIN, HU_MAX = -1024, 3071
BODY_THRESHOLD_HU = -500  # rough air/tissue boundary


@dataclass
class AnalysisOutput:
    sagittal: np.ndarray  # int16 HU, superior at the top
    geometry: SagittalGeometry
    qc: dict


def _percentile(cdf: np.ndarray, total: int, p: float) -> int:
    return int(np.searchsorted(cdf, total * p / 100.0)) + HU_MIN


def _load(path: str, orientation: Sequence[float]) -> np.ndarray:
    try:
        return load_slice_hu(path, orientation)
    except FileNotFoundError as exc:
        raise SeriesValidationError("source_file_missing", f"File was removed after indexing: {path}") from exc
    except (NotImplementedError, RuntimeError, ValueError, AttributeError) as exc:
        raise SeriesValidationError(
            "pixel_decode_failed", f"Could not decode pixel data of {Path(path).name}: {exc}"
        ) from exc


def analyze_series(
    slice_paths: Sequence[str],
    z_sorted: Sequence[float],
    *,
    rows: int,
    columns: int,
    orientation: Sequence[float],
    pixel_spacing: tuple[float, float] | None,
    slice_spacing: float,
) -> AnalysisOutput:
    z = np.asarray(z_sorted, dtype=np.float64)
    column_index = columns // 2
    profiles = np.empty((len(slice_paths), rows), dtype=np.float32)
    histogram = np.zeros(HU_MAX - HU_MIN + 1, dtype=np.int64)
    body_voxels = 0
    hu_min, hu_max = np.inf, -np.inf

    for k, path in enumerate(slice_paths):
        hu = _load(path, orientation)
        if hu.shape != (rows, columns):
            raise SeriesValidationError(
                "inconsistent_geometry", f"{Path(path).name} has shape {hu.shape}, expected {(rows, columns)}"
            )
        profiles[k] = hu[:, column_index]
        binned = np.clip(np.rint(hu), HU_MIN, HU_MAX).astype(np.int32) - HU_MIN
        histogram += np.bincount(binned.ravel(), minlength=histogram.size)
        body_voxels += int(np.count_nonzero(hu > BODY_THRESHOLD_HU))
        hu_min, hu_max = min(hu_min, float(hu.min())), max(hu_max, float(hu.max()))

    mm_per_pixel_y = pixel_spacing[0] if pixel_spacing else 1.0
    sagittal, geometry = resample_sagittal(profiles, z, mm_per_pixel_y, column_index)

    total = int(histogram.sum())
    cdf = np.cumsum(histogram)
    steps = np.diff(z)
    voxel_ml = pixel_spacing[0] * pixel_spacing[1] * slice_spacing / 1000.0 if pixel_spacing else None
    qc = {
        "hu_min": round(hu_min, 1),
        "hu_max": round(hu_max, 1),
        "hu_p01": _percentile(cdf, total, 1),
        "hu_median": _percentile(cdf, total, 50),
        "hu_p99": _percentile(cdf, total, 99),
        "z_coverage_mm": round(float(z[-1] - z[0]), 2),
        "slice_step_min_mm": round(float(steps.min()), 3),
        "slice_step_max_mm": round(float(steps.max()), 3),
        "slice_gaps": int(np.count_nonzero(steps > 1.5 * slice_spacing)),
        "body_volume_ml_estimate": round(body_voxels * voxel_ml, 1) if voxel_ml else None,
        "body_threshold_hu": BODY_THRESHOLD_HU,
    }
    return AnalysisOutput(sagittal=sagittal, geometry=geometry, qc=qc)
