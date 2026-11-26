"""What the viewer needs: series metadata, the slice for a location, and the axial and sagittal images."""

from math import isfinite

import numpy as np

from app.db import repository
from app.errors import InvalidInput, NotFound
from app.services import dicom
from app.types import LocatedRecord, SeriesRecord


def search(
    max_slice_thickness: float | None, description: str | None, before_id: int | None, limit: int
) -> list[SeriesRecord]:
    return repository.search_series(
        max_slice_thickness=max_slice_thickness, description=description, before_id=before_id, limit=limit
    )


def get_series(series_id: int) -> SeriesRecord:
    series = repository.get_series(series_id=series_id)
    if series is None:
        raise NotFound("Series not found")
    return series


def locate(z_positions: list[float], slice_spacing: float, z: float | None, index: int | None) -> LocatedRecord:
    """The slice closest to z (mm), or the slice at an index. Index 0 is the lowest z (most inferior)."""
    if (z is None) == (index is None):
        raise InvalidInput("Give either a location in mm (z) or a slice index")
    if z is not None and not isfinite(z):
        raise InvalidInput("Location must be finite")
    positions = np.asarray(z_positions)
    if index is None:
        index = int(np.argmin(np.abs(positions - z)))
    elif not 0 <= index < len(positions):
        raise InvalidInput(f"Slice index must be between 0 and {len(positions) - 1}")
    found = float(positions[index])
    half_step = slice_spacing / 2
    return {
        "index": index,
        "z": found,
        "num_slices": len(positions),
        "distance_mm": abs(found - z) if z is not None else 0.0,
        "outside": z is not None and not positions[0] - half_step <= z <= positions[-1] + half_step,
    }


def locate_in_series(series_id: int, z: float | None, index: int | None) -> LocatedRecord:
    row = repository.get_positions(series_id=series_id)
    if row is None:
        raise NotFound("Series not found")
    return locate(z_positions=row["z_positions"], slice_spacing=row["slice_spacing"], z=z, index=index)


def axial_png(series_id: int, index: int, width: float, level: float) -> bytes:
    series = get_series(series_id=series_id)
    if not series["analysis_available"]:
        raise InvalidInput("Analysis is not available; check the series job for its status and reason")
    path = repository.get_slice_file(series_id=series_id, index=index)
    if path is None:
        raise NotFound("Series or slice not found")
    # PixelSpacing is [between rows, between columns] in mm. Keep the rows and stretch the width to match,
    # so a non-square pixel is not drawn square. Validation guarantees two positive values for analyzed series.
    row_mm, column_mm = series["pixel_spacing"]
    size = (round(series["columns"] * column_mm / row_mm), series["rows"]) if row_mm != column_mm else None
    try:
        return dicom.window_png(hu=dicom.load_hu(path=path), width=width, level=level, size=size)
    except FileNotFoundError:
        raise NotFound(f"The slice file is gone: {path}") from None


def sagittal_png(series_id: int, width: float, level: float) -> bytes:
    row = repository.get_sagittal(series_id=series_id)
    if row is None:
        raise NotFound("Series not found")
    if row["sagittal"] is None:
        raise InvalidInput("Analysis is not available; check the series job for its status and reason")
    plane = np.frombuffer(row["sagittal"], dtype=np.int16).reshape(row["num_slices"], row["rows"])
    # Stretch the slice axis to true physical proportions: one image row per pixel-spacing millimetre.
    mm_per_pixel = row["pixel_spacing"][0] if row["pixel_spacing"] else 1.0
    height = max(1, round(row["num_slices"] * row["slice_spacing"] / mm_per_pixel))
    return dicom.window_png(
        hu=plane[::-1], width=width, level=level, size=(row["rows"], height)
    )  # [::-1]: superior at the top
