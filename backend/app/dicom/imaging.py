"""Pixel-level helpers: Hounsfield units, window/level, slice lookup, sagittal resampling."""

from __future__ import annotations

import bisect
import io
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np
import pydicom
from PIL import Image


def to_display_orientation(image: np.ndarray, orientation: Sequence[float]) -> np.ndarray:
    """Flip an axial image so that columns run patient-left and rows run posterior.

    Standard CT already has ImageOrientationPatient = [1,0,0, 0,1,0]; a flipped
    acquisition has negative cosines and would otherwise be shown mirrored.
    """
    if orientation[0] < 0:
        image = image[:, ::-1]
    if orientation[4] < 0:
        image = image[::-1, :]
    return image


def load_slice_hu(path: str, orientation: Sequence[float]) -> np.ndarray:
    """Read one slice and convert stored values to Hounsfield units (float32)."""
    ds = pydicom.dcmread(path)
    pixels = ds.pixel_array.astype(np.float32)
    slope = float(ds.get("RescaleSlope", 1) or 1)
    intercept = float(ds.get("RescaleIntercept", 0) or 0)
    return to_display_orientation(pixels * slope + intercept, orientation)


def apply_window(hu: np.ndarray, width: float, level: float) -> np.ndarray:
    """Linear window/level mapping to 8-bit grey: [level - width/2, level + width/2] -> [0, 255]."""
    if width <= 0:
        raise ValueError("window width must be positive")
    low = level - width / 2.0
    scaled = (hu - low) * (255.0 / width)
    return np.clip(scaled, 0, 255).astype(np.uint8)


def encode_png(image: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(image)).save(buffer, format="PNG", compress_level=3)
    return buffer.getvalue()


def closest_index(z_sorted: Sequence[float], z: float) -> int:
    """Index of the slice whose position is closest to z. z_sorted must be ascending."""
    if not z_sorted:
        raise ValueError("no slices")
    i = bisect.bisect_left(z_sorted, z)
    if i == 0:
        return 0
    if i == len(z_sorted):
        return len(z_sorted) - 1
    return i if z_sorted[i] - z < z - z_sorted[i - 1] else i - 1


@dataclass(frozen=True)
class SagittalGeometry:
    z_top: float  # patient z of the first image row (superior)
    z_bottom: float  # patient z of the last image row (inferior)
    height: int
    width: int
    mm_per_pixel_z: float
    mm_per_pixel_y: float
    column_index: int  # which axial column the plane goes through

    def row_fraction(self, z: float) -> float:
        """Vertical position of z in the image, 0 = top edge row, 1 = bottom edge row."""
        span = self.z_top - self.z_bottom
        return 0.0 if span == 0 else float(np.clip((self.z_top - z) / span, 0.0, 1.0))

    def to_dict(self) -> dict:
        return asdict(self)


def resample_sagittal(
    columns_by_slice: np.ndarray,
    z_sorted: np.ndarray,
    mm_per_pixel_y: float,
    column_index: int,
) -> tuple[np.ndarray, SagittalGeometry]:
    """Build a sagittal plane with true physical proportions.

    columns_by_slice[k] is the HU profile along y (anterior -> posterior) of the
    middle column of slice k, with slices sorted by ascending z. The output has
    superior at the top and one row per `mm_per_pixel_y` millimetres of z, so a
    2 mm slice spacing with 0.7 mm pixels is not squashed, and gaps between
    slices are linearly interpolated instead of shifting everything below them.
    """
    z = np.asarray(z_sorted, dtype=np.float64)
    z_top, z_bottom = float(z[-1]), float(z[0])
    height = max(2, int(round((z_top - z_bottom) / mm_per_pixel_y)) + 1)
    target = np.linspace(z_top, z_bottom, height)

    upper = np.clip(np.searchsorted(z, target), 1, len(z) - 1)
    z0, z1 = z[upper - 1], z[upper]
    weight = ((target - z0) / (z1 - z0))[:, None]
    plane = columns_by_slice[upper - 1] * (1 - weight) + columns_by_slice[upper] * weight

    geometry = SagittalGeometry(
        z_top=z_top,
        z_bottom=z_bottom,
        height=height,
        width=int(columns_by_slice.shape[1]),
        mm_per_pixel_z=(z_top - z_bottom) / (height - 1),
        mm_per_pixel_y=mm_per_pixel_y,
        column_index=column_index,
    )
    return np.clip(np.round(plane), -32768, 32767).astype(np.int16), geometry
