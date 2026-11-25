"""Read and validate the headers of a single-series DICOM directory.

Only headers are read here (`stop_before_pixels=True`), so indexing a 1 GB
series touches a few KB per file and finishes in seconds. Pixel work happens
later, in the analysis job.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from pydicom.multival import MultiValue

from app.errors import SeriesValidationError

log = logging.getLogger(__name__)

DICOMDIR_SOP_CLASS = "1.2.840.10008.1.3.10"
REQUIRED_IMAGE_TAGS = ("Rows", "Columns", "ImagePositionPatient", "ImageOrientationPatient")
POSITION_TOLERANCE_MM = 1e-3
IN_PLANE_TOLERANCE = 0.999  # |cosine| of rows along x and columns along y: rejects rotated or tilted planes
ORIGIN_DRIFT_TOLERANCE_MM = 1.0  # max x/y movement of slice origins across the stack
UID_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)*$")  # DICOM UI value: digits and dots, max 64 chars

# Extra header fields kept in series.attributes (JSONB) for cross-study queries.
ATTRIBUTE_KEYWORDS = (
    "Manufacturer",
    "ManufacturerModelName",
    "SoftwareVersions",
    "BodyPartExamined",
    "ProtocolName",
    "PatientPosition",
    "PatientSex",
    "PatientAge",
    "SeriesDate",
    "AcquisitionDate",
    "KVP",
    "XRayTubeCurrent",
    "Exposure",
    "ConvolutionKernel",
    "ReconstructionDiameter",
    "ContrastBolusAgent",
    "SliceLocation",
    "WindowCenter",
    "WindowWidth",
)


@dataclass(frozen=True)
class SliceInfo:
    path: str
    sop_instance_uid: str
    instance_number: int | None
    z: float


@dataclass
class SeriesInfo:
    study_instance_uid: str
    series_instance_uid: str
    patient_id: str | None
    study_description: str | None
    study_date: dt.date | None
    series_description: str | None
    series_number: int | None
    modality: str | None
    rows: int
    columns: int
    pixel_spacing: tuple[float, float] | None  # (row spacing, column spacing) in mm
    slice_thickness: float | None
    spacing_between_slices: float | None  # as written in the header
    computed_slice_spacing: float  # median step between sorted slice positions
    orientation: tuple[float, ...]
    slices: list[SliceInfo]  # sorted by z, ascending
    attributes: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    skipped_files: int = 0

    @property
    def num_slices(self) -> int:
        return len(self.slices)

    @property
    def z_min(self) -> float:
        return self.slices[0].z

    @property
    def z_max(self) -> float:
        return self.slices[-1].z


def _raise(error: OSError) -> None:
    raise error


def _iter_files(directory: Path):
    # onerror: an unreadable sub-directory must fail the job (retryable io_error), not shrink the series
    for root, dirs, files in os.walk(directory, onerror=_raise):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if not name.startswith("."):
                yield Path(root) / name


def _read_header(path: Path) -> Dataset | None:
    """Return the header of an image file, or None for anything that is not one."""
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
    except (InvalidDicomError, EOFError, ValueError, TypeError, KeyError) as exc:
        log.debug("skipping %s: %s", path, exc)
        return None
    sop_class = getattr(getattr(ds, "file_meta", None), "MediaStorageSOPClassUID", None)
    if sop_class == DICOMDIR_SOP_CLASS:
        return None
    if any(tag not in ds for tag in REQUIRED_IMAGE_TAGS):
        return None  # structured reports, presentation states, scouts without geometry...
    return ds


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    f = _float(value)
    return int(f) if f is not None else None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any) -> dt.date | None:
    text = _str(value)
    if not text or len(text) < 8:
        return None
    try:
        return dt.datetime.strptime(text[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _json_value(value: Any) -> Any:
    if isinstance(value, (MultiValue, list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, bytes):
        return None
    if isinstance(value, int):  # pydicom IS values subclass int
        return int(value)
    if isinstance(value, float):  # pydicom DS values subclass float
        return float(value)
    return str(value)  # str, PersonName, ...


def _attributes(ds: Dataset) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for keyword in ATTRIBUTE_KEYWORDS:
        value = ds.get(keyword)
        if value is None or value == "":
            continue
        converted = _json_value(value)
        if converted not in (None, "", []):
            out[keyword] = converted
    return out


def _slice_spacing_warnings(z: np.ndarray, spacing: float) -> list[str]:
    steps = np.diff(z)
    # Positions are often written with 2-3 decimals, so allow a little jitter before calling it non-uniform.
    if steps.size == 0 or steps.max() - steps.min() <= max(0.05 * spacing, 0.02):
        return []
    gaps = steps[steps > 1.5 * spacing]
    if gaps.size:
        return [
            f"{gaps.size} gap(s) in slice positions (largest {gaps.max():.2f} mm, expected {spacing:.2f} mm); "
            "missing slices are interpolated in the sagittal view"
        ]
    return [f"Non-uniform slice spacing: steps range {steps.min():.3f}-{steps.max():.3f} mm"]


def _pixel_spacing(ds: Dataset, path: Path) -> tuple[float, float] | None:
    if "PixelSpacing" not in ds:
        return None
    try:
        values = np.asarray(ds.PixelSpacing, dtype=float)
    except (TypeError, ValueError):
        raise SeriesValidationError(
            "invalid_pixel_spacing", f"{path.name}: PixelSpacing must contain two numbers"
        ) from None
    if values.shape != (2,) or not np.isfinite(values).all() or (values <= 0).any():
        raise SeriesValidationError(
            "invalid_pixel_spacing", f"{path.name}: PixelSpacing must contain two finite, positive values"
        )
    return float(values[0]), float(values[1])


def read_series_headers(directory: Path) -> SeriesInfo:
    """Index one DICOM directory. Raises SeriesValidationError with a user-facing reason."""
    if not directory.is_dir():
        raise SeriesValidationError("path_not_found", f"Directory does not exist: {directory}")

    files = list(_iter_files(directory))
    if not files:
        raise SeriesValidationError("empty_directory", f"No files in {directory}")

    by_series: dict[str, list[tuple[Path, Dataset]]] = {}
    skipped = 0
    for path in files:
        ds = _read_header(path)
        if ds is None:
            skipped += 1
            continue
        by_series.setdefault(str(ds.get("SeriesInstanceUID", "")), []).append((path, ds))

    if not by_series:
        raise SeriesValidationError(
            "no_dicom_images", f"None of the {len(files)} files in {directory} is a DICOM image with position data"
        )
    if len(by_series) > 1:
        counts = ", ".join(f"{uid or '<missing UID>'} ({len(items)} files)" for uid, items in by_series.items())
        raise SeriesValidationError("multiple_series", f"Expected exactly one series, found {len(by_series)}: {counts}")

    series_uid, items = next(iter(by_series.items()))
    if not series_uid:
        raise SeriesValidationError("missing_series_uid", "Images have no SeriesInstanceUID")
    if len(items) < 2:
        raise SeriesValidationError("too_few_slices", "A volume needs at least 2 slices")

    first = items[0][1]
    shapes = Counter((int(ds.Rows), int(ds.Columns)) for _, ds in items)
    if len(shapes) > 1:
        raise SeriesValidationError("inconsistent_geometry", f"Slices have different sizes: {dict(shapes)}")

    orientation = np.array([float(v) for v in first.ImageOrientationPatient], dtype=float)
    for path, ds in items:
        if not np.allclose([float(v) for v in ds.ImageOrientationPatient], orientation, atol=1e-3):
            raise SeriesValidationError(
                "inconsistent_geometry", f"Slice {path.name} has a different ImageOrientationPatient"
            )
    normal = np.cross(orientation[:3], orientation[3:])
    if abs(normal[2]) < 0.95:
        raise SeriesValidationError(
            "not_axial",
            f"Series is not axial (slice normal {np.round(normal, 3).tolist()}); the viewer expects axial CT",
        )
    # Display only flips axes, so rows must run along patient x and columns along y. A rotated or tilted
    # plane would otherwise turn the "sagittal" view into a coronal or sheared one.
    if abs(orientation[0]) < IN_PLANE_TOLERANCE or abs(orientation[4]) < IN_PLANE_TOLERANCE:
        raise SeriesValidationError(
            "unsupported_orientation",
            f"ImageOrientationPatient {np.round(orientation, 3).tolist()} is rotated or tilted; "
            "only standard axial orientation is supported",
        )
    origins = np.array([[float(v) for v in ds.ImagePositionPatient] for _, ds in items])
    if not np.isfinite(origins).all():
        raise SeriesValidationError("invalid_position", "ImagePositionPatient contains non-numeric values")
    drift = float(np.ptp(origins[:, :2], axis=0).max())
    if drift > ORIGIN_DRIFT_TOLERANCE_MM:
        raise SeriesValidationError(
            "position_drift",
            f"Slice origins move {drift:.1f} mm in x/y across the stack (gantry tilt or sheared volume); not supported",
        )

    slices = sorted(
        (
            SliceInfo(
                path=str(path),
                sop_instance_uid=str(ds.SOPInstanceUID),
                instance_number=_int(ds.get("InstanceNumber")),
                z=float(ds.ImagePositionPatient[2]),
            )
            for path, ds in items
        ),
        key=lambda s: s.z,
    )
    z = np.array([s.z for s in slices])
    duplicates = int(np.sum(np.diff(z) < POSITION_TOLERANCE_MM))
    if duplicates:
        raise SeriesValidationError(
            "duplicate_positions",
            f"{duplicates} slice(s) share a z position with another slice; the directory may hold several "
            "acquisitions or phases of the same series",
        )
    spacing = float(np.median(np.diff(z)))

    pixel_spacing = _pixel_spacing(first, items[0][0])
    for path, ds in items[1:]:
        current_spacing = _pixel_spacing(ds, path)
        if (current_spacing is None) != (pixel_spacing is None) or (
            pixel_spacing is not None
            and current_spacing is not None
            and not np.allclose(current_spacing, pixel_spacing, rtol=1e-5, atol=1e-6)
        ):
            raise SeriesValidationError("inconsistent_geometry", f"Slice {path.name} has a different PixelSpacing")

    warnings = _slice_spacing_warnings(z, spacing)
    modality = _str(first.get("Modality"))
    if modality != "CT":
        warnings.append(f"Modality is {modality or 'missing'}, not CT; values may not be Hounsfield units")
    if pixel_spacing is None:
        warnings.append("PixelSpacing missing; physical sizes are unknown")
    if skipped:
        warnings.append(f"Skipped {skipped} file(s) that are not DICOM images")
    if len(series_uid) > 64 or not UID_PATTERN.match(series_uid):
        # Never used in file paths (derived data is stored by numeric id), so a warning is enough.
        warnings.append(f"SeriesInstanceUID is not a standard DICOM UID: {series_uid[:80]!r}")

    return SeriesInfo(
        study_instance_uid=str(first.get("StudyInstanceUID", "")) or f"unknown-study-{series_uid}",
        series_instance_uid=series_uid,
        patient_id=_str(first.get("PatientID")),
        study_description=_str(first.get("StudyDescription")),
        study_date=_date(first.get("StudyDate")),
        series_description=_str(first.get("SeriesDescription")),
        series_number=_int(first.get("SeriesNumber")),
        modality=modality,
        rows=int(first.Rows),
        columns=int(first.Columns),
        pixel_spacing=pixel_spacing,
        slice_thickness=_float(first.get("SliceThickness")),
        spacing_between_slices=_float(first.get("SpacingBetweenSlices")),
        computed_slice_spacing=spacing,
        orientation=tuple(float(v) for v in orientation),
        slices=slices,
        attributes=_attributes(first),
        warnings=warnings,
        skipped_files=skipped,
    )
