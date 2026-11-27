"""DICOM reading and image rendering: plain functions on files and numpy arrays. No database, no HTTP."""

import io
from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
from PIL import Image
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from pydicom.pixels import apply_modality_lut

from core.errors import InvalidInput

# Every image file must carry the geometry the viewer needs; one without it is reported, not skipped.
REQUIRED_TAGS = ("SeriesInstanceUID", "ImagePositionPatient", "ImageOrientationPatient", "Rows", "Columns")

# The only non-DICOM files a series folder may contain; any other unreadable file fails the series.
ANCILLARY_SUFFIXES = frozenset(
    {".txt", ".md", ".pdf", ".csv", ".json", ".xml", ".html", ".htm", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".log"}
)
ANCILLARY_NAMES = frozenset({"thumbs.db", "desktop.ini"})  # written by file browsers

# Allocation limits. A header can claim any size, so sizes are checked before pixels are decoded or drawn.
MAX_FRAME_PIXELS = 2048 * 2048  # one decoded slice, Rows x Columns (CT is usually 512 x 512)
MAX_RENDER_SIDE = 4096  # either side of a PNG after stretching to physical proportions

# The byte format of a stored sagittal plane, kept next to it in series.sagittal_encoding.
SAGITTAL_ENCODINGS = {
    "float32le": np.dtype("<f4"),  # every new plane: keeps fractional HU from RescaleSlope/Intercept
    "int16le": np.dtype("<i2"),  # planes written before float32 (HU truncated to whole numbers)
}
SAGITTAL_ENCODING = "float32le"


@dataclass(frozen=True, slots=True)
class SliceHeader:
    """The per-file tags validate_geometry checks, kept from the one header read (values as pydicom gives them)."""

    position: Any  # ImagePositionPatient
    orientation: Any  # ImageOrientationPatient
    modality: str | None
    frames: Any  # NumberOfFrames, default 1
    photometric: str | None
    rows: int
    columns: int
    pixel_spacing: Any  # [] when absent


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
    headers: list[SliceHeader] = field(default_factory=list, repr=False)  # same order; not stored in the database


def read_series(directory: Path, *, validate: bool = True) -> SeriesInfo:
    """Extract headers; ingestion defers geometry validation until metadata has been committed."""
    slices: list[tuple[float, Path, SliceHeader]] = []
    first: Dataset | None = None  # series-level tags come from the first image file in name order
    series_uids: set[str] = set()
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
        except (OSError, MemoryError):
            raise  # storage or memory trouble, not bad data: the job decides whether to retry
        except Exception as exc:
            raise InvalidInput(f"{path.name}: unreadable DICOM header ({type(exc).__name__}: {exc})") from None
        if not is_image(ds=ds):
            continue  # a DICOM object without pixels: a DICOMDIR, a structured report, ...
        # pydicom returns a truncated file's header without complaint: a dropped end slice would still look regular.
        if missing := [tag for tag in REQUIRED_TAGS if tag not in ds]:
            raise InvalidInput(f"{path.name} is missing {', '.join(missing)} (truncated or incomplete image?)")
        if first is None:
            first = ds
        series_uids.add(ds.SeriesInstanceUID)
        header = SliceHeader(
            position=ds.ImagePositionPatient,
            orientation=ds.ImageOrientationPatient,
            modality=ds.get("Modality"),
            frames=ds.get("NumberOfFrames", 1),
            photometric=ds.get("PhotometricInterpretation"),
            rows=int(ds.Rows),
            columns=int(ds.Columns),
            pixel_spacing=ds.get("PixelSpacing", []),
        )
        slices.append((float(ds.ImagePositionPatient[2]), path, header))

    if first is None:
        raise InvalidInput(f"No DICOM images with position data in {directory.name}")
    if len(series_uids) > 1:
        raise InvalidInput(f"Expected one series per directory, found {len(series_uids)}")
    if len(slices) < 2:
        raise InvalidInput("A volume needs at least 2 slices")

    # Volume order comes from the patient position, never from file names or InstanceNumber.
    slices.sort(key=lambda item: item[0])
    z = [position for position, _, _ in slices]
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
        files=[str(path) for _, path, _ in slices],
        headers=[header for _, _, header in slices],
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
    check_frame_size(rows=info.rows, columns=info.columns)  # every slice is checked to match below
    origin = None
    for header in info.headers:
        orientation = np.asarray(header.orientation, dtype=float)
        if orientation.shape != (6,) or not np.allclose(orientation, [1, 0, 0, 0, 1, 0], atol=1e-4, rtol=0):
            raise InvalidInput(
                "The series is not axial in the supported orientation; oblique or flipped data needs resampling"
            )
        if header.modality != "CT" or int(header.frames) != 1:
            raise InvalidInput("The viewer supports single-frame CT images only")
        if header.photometric != "MONOCHROME2":
            raise InvalidInput("The viewer supports MONOCHROME2 images only")
        if (header.rows, header.columns) != (info.rows, info.columns):
            raise InvalidInput("Slices have inconsistent image dimensions")
        current_spacing = np.asarray(header.pixel_spacing, dtype=float)
        if current_spacing.shape != (2,) or not np.allclose(current_spacing, spacing):
            raise InvalidInput("Slices have inconsistent pixel spacing")
        xy = np.asarray(header.position[:2], dtype=float)
        if origin is None:
            origin = xy
        if not np.all(np.isfinite(xy)) or not np.allclose(xy, origin, atol=0.01, rtol=0):
            raise InvalidInput("Slice origins are shifted; this volume requires resampling")


def check_frame_size(rows: int, columns: int) -> None:
    # Each side is bounded too: a 65535 x 64 slice fits the pixel count but could never be drawn.
    if not (0 < rows <= MAX_RENDER_SIDE and 0 < columns <= MAX_RENDER_SIDE and rows * columns <= MAX_FRAME_PIXELS):
        raise InvalidInput(
            f"Images of {rows} x {columns} pixels are not supported "
            f"(at most {MAX_FRAME_PIXELS:,} pixels per slice and {MAX_RENDER_SIDE} per side)"
        )


def load_hu(path: str) -> np.ndarray:
    """One slice in Hounsfield units (RescaleSlope and RescaleIntercept applied)."""
    ds = pydicom.dcmread(fp=path)  # reads the pixel bytes; decoding waits for .pixel_array
    check_frame_size(rows=int(ds.Rows), columns=int(ds.Columns))
    if int(ds.get("NumberOfFrames", 1)) != 1:
        raise InvalidInput("The viewer supports single-frame CT images only")
    return apply_modality_lut(arr=ds.pixel_array, ds=ds)


def sagittal(files: list[str], rows: int, columns: int, *, check: Callable[[], None] | None = None) -> np.ndarray:
    """The analysis: one pass over the pixels, keeping the middle column of every slice -> (num_slices, rows).

    Stored as SAGITTAL_ENCODING (float32): fractional HU stay as they are, as in the axial view."""
    plane = np.empty((len(files), rows), dtype=SAGITTAL_ENCODINGS[SAGITTAL_ENCODING])
    for i, path in enumerate(files):
        if check is not None:
            check()
        try:
            hu = load_hu(path=path)
        except (OSError, MemoryError, InvalidInput):
            raise  # storage or memory trouble is for the job to retry; InvalidInput already has its reason
        except Exception as exc:  # e.g. pixel data cut short, which a header-only read cannot notice
            raise InvalidInput(
                f"{Path(path).name}: cannot decode the pixel data ({type(exc).__name__}: {exc})"
            ) from None
        if hu.shape != (rows, columns):
            raise InvalidInput(f"{Path(path).name} has a different image size than the first slice")
        plane[i] = hu[:, columns // 2]
    return plane


def decode_plane(data: bytes, encoding: str | None, num_slices: int, rows: int) -> np.ndarray:
    """A stored sagittal plane, read in the format it was written in (never old int16 bytes as float32)."""
    dtype = SAGITTAL_ENCODINGS.get(encoding or "")
    if dtype is None or len(data) != num_slices * rows * dtype.itemsize:
        raise InvalidInput("The stored sagittal plane is unreadable; index the series again")
    return np.frombuffer(data, dtype=dtype).reshape(num_slices, rows)


def render_size(width: float, height: float) -> tuple[int, int]:
    """(width, height) in pixels of an image drawn to physical proportions, checked before Pillow allocates it."""
    if not (isfinite(width) and isfinite(height)):
        raise InvalidInput("The pixel or slice spacing gives the image no finite size")
    size = (round(width), round(height))
    if not all(1 <= side <= MAX_RENDER_SIDE for side in size):
        raise InvalidInput(
            f"Drawn to scale the image would be {size[0]} x {size[1]} pixels; at most {MAX_RENDER_SIDE} per side "
            "and at least 1 are supported (extreme pixel or slice spacing?)"
        )
    return size


def window_png(hu: np.ndarray, width: float, level: float, size: tuple[int, int] | None = None) -> bytes:
    """Window/level to 8-bit grey (level - width/2 is black, level + width/2 is white), optionally resized, as PNG."""
    if not isfinite(width) or width <= 0 or not isfinite(level):
        raise InvalidInput("Window width must be positive and finite; level must be finite")
    grey = hu.astype(np.float32)  # one working copy, then in place; float32 holds int16 HU and float32 planes
    grey -= level - width / 2
    grey *= 255
    grey /= width  # after the multiply, so level + width/2 lands on exactly 255
    np.clip(grey, 0, 255, out=grey)
    image = Image.fromarray(grey.astype(np.uint8))
    if size and size != image.size:
        image = image.resize(size=size, resample=Image.Resampling.BILINEAR)
    buffer = io.BytesIO()
    image.save(fp=buffer, format="PNG")
    return buffer.getvalue()
