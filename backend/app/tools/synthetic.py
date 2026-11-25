"""Generate a synthetic, fully anonymous CT series for demos and tests.

The phantom is a tapered body ellipse with lungs, a spine, a heart and a small
spherical "lesion", written as one DICOM file per axial slice. File names are
random UIDs (like the sample data), so readers must sort by position, not name.

    python -m app.tools.synthetic /data/library/synthetic_chest --slices 200
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid


def _phantom_slice(x: np.ndarray, y: np.ndarray, z: float, z_extent: float, rng: np.random.Generator) -> np.ndarray:
    """Return one axial slice in HU. x/y are patient coordinates in mm (LPS)."""
    t = z / z_extent  # -0.5 (feet) .. 0.5 (head)
    taper = 1.0 - 0.6 * max(0.0, abs(t) - 0.3) / 0.2 if abs(t) > 0.3 else 1.0
    hu = np.full(x.shape, -1000.0, dtype=np.float32)

    body = (x / (150 * taper)) ** 2 + (y / (110 * taper)) ** 2 <= 1
    hu[body] = 40.0
    if t > -0.1:  # thorax: lungs and heart
        for cx in (-70.0, 70.0):
            hu[((x - cx) / 50) ** 2 + ((y + 10) / 70) ** 2 <= 1] = -850.0
        hu[((x - 20) / 45) ** 2 + ((y + 20) / 40) ** 2 <= 1] = 50.0
    else:  # abdomen: liver
        hu[((x + 55) / 60) ** 2 + ((y + 5) / 55) ** 2 <= 1] = 60.0
    hu[(x**2 + (y - 70) ** 2) <= 18**2] = 700.0  # vertebral body
    # A sphere on the midline: it must look round in the sagittal view if the aspect ratio is right.
    lesion_r2 = 20**2 - (z - 0.25 * z_extent) ** 2
    if lesion_r2 > 0:
        hu[(x**2 + (y + 80) ** 2) <= lesion_r2] = 150.0

    hu[body] += rng.normal(0, 8, size=int(body.sum()))
    return hu


def write_series(
    out_dir: Path,
    *,
    slices: int = 200,
    size: int = 512,
    pixel_spacing: float = 0.7,
    slice_spacing: float = 2.0,
    slice_thickness: float | None = None,
    study_description: str = "CHEST ABDOMEN (SYNTHETIC)",
    series_description: str = "Synthetic phantom",
    patient_id: str = "ANON-SYN-0001",
    study_uid: str | None = None,
    drop_slices: tuple[int, ...] = (),
    seed: int = 7,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    study_uid = study_uid or generate_uid()
    series_uid = generate_uid()
    frame_uid = generate_uid()
    today = dt.date.today().strftime("%Y%m%d")

    half = (size - 1) * pixel_spacing / 2
    coords = np.arange(size) * pixel_spacing - half
    x, y = np.meshgrid(coords, coords)  # rows run along y, columns along x
    z_extent = (slices - 1) * slice_spacing
    z_top = z_extent / 2

    for i in range(slices):
        if i in drop_slices:
            continue
        z = z_top - i * slice_spacing  # instance 1 is the most superior slice (head first)
        hu = _phantom_slice(x, y, z, max(z_extent, 1.0), rng)
        stored = np.clip(np.round(hu) + 1024, 0, 4095).astype(np.uint16)

        sop_uid = generate_uid()
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = Dataset()
        ds.file_meta = meta
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.PatientName = "Anonymous"
        ds.PatientID = patient_id
        ds.StudyDate = ds.SeriesDate = today
        ds.StudyDescription = study_description
        ds.SeriesDescription = series_description
        ds.Modality = "CT"
        ds.Manufacturer = "Synthetic"
        ds.BodyPartExamined = "CHEST"
        ds.KVP = 120
        ds.ConvolutionKernel = "B"
        ds.PatientPosition = "HFS"
        ds.SeriesNumber = 2
        ds.InstanceNumber = i + 1
        ds.ImagePositionPatient = [-half, -half, round(z, 3)]
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.SliceLocation = round(z, 3)
        ds.PixelSpacing = [pixel_spacing, pixel_spacing]
        ds.SliceThickness = slice_thickness if slice_thickness is not None else slice_spacing
        ds.Rows = ds.Columns = size
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 12
        ds.HighBit = 11
        ds.PixelRepresentation = 0
        ds.RescaleIntercept = -1024
        ds.RescaleSlope = 1
        ds.RescaleType = "HU"
        ds.WindowCenter = 40
        ds.WindowWidth = 400
        ds.PixelData = stored.tobytes()
        ds.save_as(out_dir / f"{sop_uid}.dcm", enforce_file_format=True)
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--slices", type=int, default=200)
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--pixel-spacing", type=float, default=0.7)
    parser.add_argument("--slice-spacing", type=float, default=2.0)
    parser.add_argument("--slice-thickness", type=float, default=None)
    parser.add_argument("--series-description", default="Synthetic phantom")
    parser.add_argument("--study-description", default="CHEST ABDOMEN (SYNTHETIC)")
    args = parser.parse_args()
    created = not args.out_dir.exists()
    path = write_series(
        args.out_dir,
        slices=args.slices,
        size=args.size,
        pixel_spacing=args.pixel_spacing,
        slice_spacing=args.slice_spacing,
        slice_thickness=args.slice_thickness,
        series_description=args.series_description,
        study_description=args.study_description,
    )
    if created and hasattr(os, "geteuid") and os.geteuid() == 0:
        _give_to_parent_owner(path)
    print(f"Wrote {args.slices} slices to {path}")


def _give_to_parent_owner(path: Path) -> None:
    """Run as root into a bind mount, hand the files to whoever owns the host folder."""
    parent = path.parent.stat()
    for item in [path, *path.rglob("*")]:
        os.chown(item, parent.st_uid, parent.st_gid)


if __name__ == "__main__":
    main()
