"""What the viewer needs: series metadata, the slice for a location, and the axial and sagittal images."""

from bisect import bisect_left
from datetime import datetime
from math import isfinite

from core.errors import Conflict, InvalidInput, NotFound
from core.types import AxialRecord, LocatedRecord, PositionRecord, SagittalRecord, SeriesRecord
from db import repository
from services import dicom


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
    if not z_positions:
        raise InvalidInput("The series has no slice positions")
    last = len(z_positions) - 1
    if index is None:
        above = bisect_left(z_positions, z)  # ascending: the first slice at or above z
        if above == 0 or above > last:
            index = min(above, last)  # beyond an end: that end slice
        else:  # between two slices; <= makes a tie pick the lower index
            index = above - 1 if z - z_positions[above - 1] <= z_positions[above] - z else above
    elif not 0 <= index <= last:
        raise InvalidInput(f"Slice index must be between 0 and {last}")
    found = z_positions[index]
    half_step = slice_spacing / 2
    return {
        "index": index,
        "z": found,
        "num_slices": len(z_positions),
        "distance_mm": abs(found - z) if z is not None else 0.0,
        "outside": z is not None and not z_positions[0] - half_step <= z <= z_positions[-1] + half_step,
    }


def current[R: (PositionRecord, AxialRecord, SagittalRecord)](row: R | None) -> R:
    """The row, if it exists and is still the version the client asked for (the query compared updated_at)."""
    if row is None:
        raise NotFound("Series not found")
    if not row["current"]:
        raise Conflict("The series was indexed again since it was loaded; reload it")
    return row


def locate_in_series(series_id: int, z: float | None, index: int | None, version: datetime | None) -> LocatedRecord:
    row = current(repository.get_positions(series_id=series_id, version=version))
    return locate(z_positions=row["z_positions"], slice_spacing=row["slice_spacing"], z=z, index=index)


def axial_png(series_id: int, index: int, width: float, level: float, version: datetime | None) -> bytes:
    series = current(repository.get_axial(series_id=series_id, index=index, version=version))
    if not series["analysis_available"]:
        raise InvalidInput("Analysis is not available; check the series job for its status and reason")
    path = series["file"]
    if path is None:
        raise NotFound("Slice not found")
    # PixelSpacing is [between rows, between columns] in mm. Keep the rows and stretch the width to match,
    # so a non-square pixel is not drawn square. Validation guarantees two positive values for analyzed series.
    row_mm, column_mm = series["pixel_spacing"]
    size = dicom.render_size(width=series["columns"] * column_mm / row_mm, height=series["rows"])
    try:
        return dicom.window_png(hu=dicom.load_hu(path=path), width=width, level=level, size=size)
    except FileNotFoundError:
        raise NotFound(f"The slice file is gone: {path}") from None


def sagittal_png(series_id: int, width: float, level: float, version: datetime | None) -> bytes:
    row = current(repository.get_sagittal(series_id=series_id, version=version))
    if row["sagittal"] is None:
        raise InvalidInput("Analysis is not available; check the series job for its status and reason")
    # Stretch the slice axis to true physical proportions: one image row per pixel-spacing millimetre.
    mm_per_pixel = row["pixel_spacing"][0] if row["pixel_spacing"] else 1.0
    size = dicom.render_size(width=row["rows"], height=row["num_slices"] * row["slice_spacing"] / mm_per_pixel)
    plane = dicom.decode_plane(
        data=row["sagittal"], encoding=row["sagittal_encoding"], num_slices=row["num_slices"], rows=row["rows"]
    )
    return dicom.window_png(hu=plane[::-1], width=width, level=level, size=size)  # [::-1]: superior at the top
