"""Test data: tiny synthetic CT series written with pydicom (no real patient data needed, also in CI)."""

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid


def write_slice(
    path, z, *, series_uid="1.2.826.0.1.1", value=0, size=8, orientation=(1, 0, 0, 0, 1, 0), pixel_spacing=(0.5, 0.5)
):
    """One 8x8 CT slice at height z whose stored pixels all equal `value` (HU = value - 1024)."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = "1.2.826.0.1"
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = "ANON01"
    ds.Modality = "CT"
    ds.StudyDescription = "CHEST"
    ds.SeriesDescription = "AXIAL 1MM"
    ds.ImagePositionPatient = [0, 0, z]
    ds.ImageOrientationPatient = list(orientation)
    ds.PixelSpacing = list(pixel_spacing)
    ds.SliceThickness = 1.0
    ds.Rows = ds.Columns = size
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.RescaleSlope = 1
    ds.RescaleIntercept = -1024
    ds.PixelData = np.full((size, size), value, dtype=np.int16).tobytes()
    ds.save_as(path, enforce_file_format=True)


@pytest.fixture
def make_series(tmp_path):
    """make_series("name", n) -> directory with n slices from z = -100 upward; slice i has pixel value 10 * i."""

    def make(name="series", n=5, step=2.5):
        directory = tmp_path / name
        directory.mkdir()
        for i in range(n):
            # File names run opposite to z: reading in name order would give a reversed volume.
            write_slice(directory / f"{n - i:03}.dcm", z=-100 + i * step, value=10 * i)
        return directory

    return make
