"""DICOM reading and image rendering: plain functions on files and numpy arrays. No database, no HTTP."""

import io
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

import numpy as np
import pydicom
from PIL import Image
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from pydicom.pixels import apply_modality_lut

from app.errors import InvalidInput

# Every image file must carry the geometry the viewer needs; one without it is reported, not skipped.
REQUIRED_TAGS = ("SeriesInstanceUID", "ImagePositionPatient", "ImageOrientationPatient", "Rows", "Columns")

# The only non-DICOM files a series folder may contain; any other unreadable file fails the series.
ANCILLARY_SUFFIXES = frozenset(
    {".txt", ".md", ".pdf", ".csv", ".json", ".xml", ".html", ".htm", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".log"}
)
ANCILLARY_NAMES = frozenset({"thumbs.db", "desktop.ini"})  # written by file browsers


@dataclass(frozen=True, slots=True)
class SeriesInfo:
    series_uid: str
    study_uid: str
    patient_id: str | None
    study_description: str | None
    series_description: str | None
    modality: str | None
    pixel_spacing: list[float] | None  # [row, column] mm
    slice_thickness: float | None
    spacing_between_slices: float | None  # the header tag, as written
    slice_spacing: float  # measured: median distance between slice positions
    rows: int
    columns: int
    z_positions: list[float]  # ascending: index 0 is the most inferior slice
    files: list[str]  # same order as z_positions


def read_series(directory: Path, *, validate: bool = True) -> SeriesInfo:
    """Extract headers; ingestion defers geometry validation until metadata has been committed."""
    slices: list[tuple[Path, Dataset]] = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        try:
            ds = pydicom.dcmread(fp=path, stop_before_pixels=True)
        except InvalidDicomError:
            if is_ancillary(path=path):
                continue
            # DICOM files often have no extension, so any other unreadable file may be a slice cut short.
            raise InvalidInput(
                f"{path.name} is not a readable DICOM file (empty or truncated?). "
                f"Besides DICOM, a series folder may hold only {', '.join(sorted(ANCILLARY_SUFFIXES))} files"
            ) from None
        except Exception as exc:
            raise InvalidInput(f"{path.name}: unreadable DICOM header ({type(exc).__name__}: {exc})") from None
        if not is_image(ds=ds):
            continue  # a DICOM object without pixels: a DICOMDIR, a structured report, ...
        # pydicom returns a truncated file's header without complaint: a dropped end slice would still look regular.
        if missing := [tag for tag in REQUIRED_TAGS if tag not in ds]:
            raise InvalidInput(f"{path.name} is missing {', '.join(missing)} (truncated or incomplete image?)")
        slices.append((path, ds))

    if not slices:
        raise InvalidInput(f"No DICOM images with position data in {directory.name}")
    series_uids = {ds.SeriesInstanceUID for _, ds in slices}
    if len(series_uids) > 1:
        raise InvalidInput(f"Expected one series per directory, found {len(series_uids)}")
    if len(slices) < 2:
        raise InvalidInput("A volume needs at least 2 slices")

    first = slices[0][1]

    # Volume order comes from the patient position, never from file names or InstanceNumber.
    slices.sort(key=lambda item: float(item[1].ImagePositionPatient[2]))
    z = [float(ds.ImagePositionPatient[2]) for _, ds in slices]
    steps = np.diff(z)

    def number(keyword: str) -> float | None:
        value = first.get(keyword)
        return float(value) if value not in (None, "") else None

    info = SeriesInfo(
        series_uid=str(first.SeriesInstanceUID),
        study_uid=str(first.get("StudyInstanceUID", "")),
        patient_id=first.get("PatientID") or None,
        study_description=first.get("StudyDescription") or None,
        series_description=first.get("SeriesDescription") or None,
        modality=first.get("Modality") or None,
        pixel_spacing=[float(v) for v in first.PixelSpacing] if "PixelSpacing" in first else None,
        slice_thickness=number(keyword="SliceThickness"),
        spacing_between_slices=number(keyword="SpacingBetweenSlices"),
        slice_spacing=float(np.median(steps)),
        rows=int(first.Rows),
        columns=int(first.Columns),
        z_positions=z,
        files=[str(path) for path, _ in slices],
    )
    if validate:
        validate_geometry(info=info)
    return info


def is_ancillary(*, path: Path) -> bool:
    """A document, picture or system file (.DS_Store and other dotfiles) filed next to the slices."""
    name = path.name.lower()
    return path.suffix.lower() in ANCILLARY_SUFFIXES or name in ANCILLARY_NAMES or name.startswith(".")


def is_image(*, ds: Dataset) -> bool:
    """Image instances must be complete slices. A file whose SOP class is unreadable counts as a broken image."""
    sop_class = ds.get("SOPClassUID") or ds.file_meta.get("MediaStorageSOPClassUID")
    return "Rows" in ds or sop_class is None or "Image" in sop_class.name


def validate_geometry(*, info: SeriesInfo) -> None:
    """Reject geometry that a middle-column reconstruction cannot represent accurately."""
    positions = np.asarray(info.z_positions, dtype=float)
    steps = np.diff(positions)
    if not np.all(np.isfinite(positions)):
        raise InvalidInput("Slice positions must be finite")
    if np.any(steps < 1e-3):
        raise InvalidInput("Several slices share one z position (more than one acquisition in the directory?)")
    if not np.allclose(steps, info.slice_spacing, rtol=0.01, atol=0.01):
        raise InvalidInput("Irregular slice spacing requires resampling, which this viewer does not support")
    spacing = np.asarray(info.pixel_spacing, dtype=float)
    if spacing.shape != (2,) or not np.all(np.isfinite(spacing)) or np.any(spacing <= 0):
        raise InvalidInput("Pixel spacing must contain two positive finite values")
    origin = None
    for path in info.files:
        ds = pydicom.dcmread(fp=path, stop_before_pixels=True)
        orientation = np.asarray(ds.ImageOrientationPatient, dtype=float)
        if orientation.shape != (6,) or not np.allclose(orientation, [1, 0, 0, 0, 1, 0], atol=1e-4, rtol=0):
            raise InvalidInput(
                "The series is not axial in the supported orientation; oblique or flipped data needs resampling"
            )
        if ds.get("Modality") != "CT" or int(ds.get("NumberOfFrames", 1)) != 1:
            raise InvalidInput("The viewer supports single-frame CT images only")
        if ds.get("PhotometricInterpretation") != "MONOCHROME2":
            raise InvalidInput("The viewer supports MONOCHROME2 images only")
        if (int(ds.Rows), int(ds.Columns)) != (info.rows, info.columns):
            raise InvalidInput("Slices have inconsistent image dimensions")
        current_spacing = np.asarray(ds.get("PixelSpacing", []), dtype=float)
        if current_spacing.shape != (2,) or not np.allclose(current_spacing, spacing):
            raise InvalidInput("Slices have inconsistent pixel spacing")
        xy = np.asarray(ds.ImagePositionPatient[:2], dtype=float)
        if origin is None:
            origin = xy
        if not np.all(np.isfinite(xy)) or not np.allclose(xy, origin, atol=0.01, rtol=0):
            raise InvalidInput("Slice origins are shifted; this volume requires resampling")


def load_hu(path: str) -> np.ndarray:
    """One slice in Hounsfield units (RescaleSlope and RescaleIntercept applied)."""
    ds = pydicom.dcmread(fp=path)
    return apply_modality_lut(arr=ds.pixel_array, ds=ds)


def sagittal(files: list[str], rows: int, columns: int, *, check: Callable[[], None] | None = None) -> np.ndarray:
    """The analysis: one pass over the pixels, keeping the middle column of every slice -> (num_slices, rows), int16."""
    plane = np.empty((len(files), rows), dtype=np.int16)
    for i, path in enumerate(files):
        if check is not None:
            check()
        try:
            hu = load_hu(path=path)
        except Exception as exc:  # e.g. pixel data cut short, which a header-only read cannot notice
            raise InvalidInput(
                f"{Path(path).name}: cannot decode the pixel data ({type(exc).__name__}: {exc})"
            ) from None
        if hu.shape != (rows, columns):
            raise InvalidInput(f"{Path(path).name} has a different image size than the first slice")
        plane[i] = np.clip(hu[:, columns // 2], -32768, 32767)
    return plane


def window_png(hu: np.ndarray, width: float, level: float, size: tuple[int, int] | None = None) -> bytes:
    """Window/level to 8-bit grey (level - width/2 is black, level + width/2 is white), optionally resized, as PNG."""
    if not isfinite(width) or width <= 0 or not isfinite(level):
        raise InvalidInput("Window width must be positive and finite; level must be finite")
    grey = np.clip((hu.astype(np.float64) - (level - width / 2)) / width * 255, 0, 255).astype(np.uint8)
    image = Image.fromarray(grey)
    if size:
        image = image.resize(size=size, resample=Image.Resampling.BILINEAR)
    buffer = io.BytesIO()
    image.save(fp=buffer, format="PNG")
    return buffer.getvalue()
