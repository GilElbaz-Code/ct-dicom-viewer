import io
from pathlib import Path

import numpy as np
import pydicom
import pytest
from PIL import Image
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import BasicTextSRStorage, ExplicitVRLittleEndian, generate_uid

from app.errors import InvalidInput
from app.services import dicom
from tests.conftest import write_slice


def test_reads_headers_and_sorts_by_position_not_file_name(make_series):
    info = dicom.read_series(make_series(n=5, step=2.5))
    assert info.z_positions == [-100, -97.5, -95, -92.5, -90]
    assert [Path(f).name for f in info.files] == ["005.dcm", "004.dcm", "003.dcm", "002.dcm", "001.dcm"]
    assert info.slice_spacing == 2.5
    assert info.spacing_between_slices is None  # tag absent: the UI shows the measured spacing instead
    assert (info.slice_thickness, info.pixel_spacing, info.rows, info.modality) == (1.0, [0.5, 0.5], 8, "CT")


def test_skips_files_that_are_not_images(make_series):
    directory = make_series(n=3)
    for name in ("README.txt", "notes.pdf", ".DS_Store", "Thumbs.db"):
        (directory / name).write_text("not an image")
    report = Dataset()  # a DICOM object without pixels, e.g. a structured report filed with the series
    report.file_meta = FileMetaDataset()
    report.file_meta.MediaStorageSOPClassUID = report.SOPClassUID = BasicTextSRStorage
    report.file_meta.MediaStorageSOPInstanceUID = report.SOPInstanceUID = generate_uid()
    report.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    report.SeriesInstanceUID = generate_uid()
    report.save_as(directory / "report.dcm", enforce_file_format=True)
    assert len(dicom.read_series(directory).files) == 3


# make_series names files opposite to z: 005.dcm is the lowest slice, 001.dcm the highest.
@pytest.mark.parametrize("keep", [0.1, 0.3, 0.5, 0.7])  # no preamble; no tags; SOP class only; no Rows
def test_a_truncated_end_slice_fails_the_series_instead_of_shortening_it(make_series, keep):
    directory = make_series(n=5)
    top = directory / "001.dcm"
    data = top.read_bytes()
    top.write_bytes(data[: int(len(data) * keep)])
    with pytest.raises(InvalidInput, match="001.dcm"):
        dicom.read_series(directory)


@pytest.mark.parametrize("keep", [0.1, 0.3])  # too short to be recognized as DICOM; recognized, but without tags
def test_a_truncated_end_slice_without_an_extension_fails_too(tmp_path, keep):
    for i in range(5):  # IM0000 is the lowest slice, IM0004 the highest, as many scanners name them
        write_slice(tmp_path / f"IM{i:04}", z=-100 + 2.5 * i)
    top = tmp_path / "IM0004"
    data = top.read_bytes()
    top.write_bytes(data[: int(len(data) * keep)])
    with pytest.raises(InvalidInput, match="IM0004"):
        dicom.read_series(tmp_path)


def test_an_image_without_position_is_reported_by_name(make_series):
    directory = make_series(n=5)
    lowest = directory / "005.dcm"
    ds = pydicom.dcmread(fp=lowest)
    del ds.ImagePositionPatient
    ds.save_as(lowest, enforce_file_format=True)
    with pytest.raises(InvalidInput, match="005.dcm is missing ImagePositionPatient"):
        dicom.read_series(directory)


def test_truncated_pixel_data_is_reported_by_name(make_series):
    directory = make_series(n=5)
    top = directory / "001.dcm"
    top.write_bytes(top.read_bytes()[:-20])  # the header is intact, so only decoding can notice
    info = dicom.read_series(directory)
    with pytest.raises(InvalidInput, match="001.dcm: cannot decode the pixel data"):
        dicom.sagittal(files=info.files, rows=info.rows, columns=info.columns)


UNUSABLE = {
    "No DICOM images": lambda d: None,
    "one series per directory": lambda d: [
        write_slice(d / "a.dcm", 0, series_uid="1.1"),
        write_slice(d / "b.dcm", 1, series_uid="1.2"),
    ],
    "at least 2 slices": lambda d: write_slice(d / "a.dcm", 0),
    "not axial": lambda d: [write_slice(d / f"{z}.dcm", z, orientation=(1, 0, 0, 0, 0, -1)) for z in (0, 1)],
    "share one z position": lambda d: [write_slice(d / f"{i}.dcm", 0) for i in (1, 2)],
}


@pytest.mark.parametrize("reason", UNUSABLE)
def test_rejects_unusable_directories_with_a_reason(tmp_path, reason):
    UNUSABLE[reason](tmp_path)
    with pytest.raises(InvalidInput, match=reason):
        dicom.read_series(tmp_path)


def test_sagittal_keeps_the_middle_column_of_each_slice_in_z_order(make_series):
    info = dicom.read_series(make_series(n=3))
    plane = dicom.sagittal(info.files, info.rows, info.columns)
    assert plane.shape == (3, 8)
    assert plane[:, 0].tolist() == [-1024, -1014, -1004]  # HU = 10 * i - 1024, lowest z first


def test_window_maps_level_minus_and_plus_half_width_to_black_and_white():
    png = dicom.window_png(np.array([[-160.0, 40.0, 240.0, 1000.0]]), width=400, level=40)
    assert np.array(Image.open(io.BytesIO(png))).tolist() == [[0, 127, 255, 255]]


@pytest.mark.parametrize(
    ("tag", "value", "reason"),
    [
        ("ImageOrientationPatient", [-1, 0, 0, 0, -1, 0], "orientation"),
        ("PixelSpacing", [0, 0.5], "spacing"),
        ("ImagePositionPatient", [1, 0, -95], "origins"),
        ("PhotometricInterpretation", "MONOCHROME1", "MONOCHROME2"),
        ("Modality", "MR", "single-frame CT"),
    ],
)
def test_rejects_unsupported_geometry_after_extracting_headers(make_series, tag, value, reason):
    directory = make_series(n=3)
    path = directory / "001.dcm"
    ds = pydicom.dcmread(fp=path)
    setattr(ds, tag, value)
    ds.save_as(path, enforce_file_format=True)  # pydicom requires filename positionally
    info = dicom.read_series(directory=directory, validate=False)
    assert info.patient_id == "ANON01"
    with pytest.raises(InvalidInput, match=reason):
        dicom.validate_geometry(info=info)


def test_rejects_irregular_slice_spacing(make_series):
    directory = make_series(n=3)
    path = directory / "001.dcm"
    ds = pydicom.dcmread(fp=path)
    ds.ImagePositionPatient = [0, 0, -93]
    ds.save_as(path, enforce_file_format=True)  # pydicom requires filename positionally
    with pytest.raises(InvalidInput, match="Irregular"):
        dicom.read_series(directory=directory)


@pytest.mark.parametrize(("width", "level"), [(0, 40), (-1, 40), (float("nan"), 40), (400, float("inf"))])
def test_window_rejects_invalid_parameters(width, level):
    with pytest.raises(InvalidInput, match="finite"):
        dicom.window_png(hu=np.zeros((2, 2)), width=width, level=level)
