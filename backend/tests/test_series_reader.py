from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from app.dicom.series import read_series_headers
from app.errors import SeriesValidationError
from app.tools.synthetic import write_series


def test_reads_and_sorts_by_position_not_file_name(small_series: Path) -> None:
    info = read_series_headers(small_series)
    z = [s.z for s in info.slices]
    assert info.num_slices == 40
    assert z == sorted(z)
    assert info.z_min == pytest.approx(-48.75) and info.z_max == pytest.approx(48.75)
    # synthetic data writes instance 1 at the top (head first), so instance numbers run backwards
    assert info.slices[0].instance_number == 40 and info.slices[-1].instance_number == 1


def test_series_metadata(small_series: Path) -> None:
    info = read_series_headers(small_series)
    assert info.modality == "CT"
    assert info.rows == info.columns == 64
    assert info.pixel_spacing == (5.0, 5.0)
    assert info.slice_thickness == 1.0
    assert info.spacing_between_slices is None  # tag absent, as in much real CT data
    assert info.computed_slice_spacing == pytest.approx(2.5)
    assert info.patient_id == "ANON-SYN-0001"
    assert info.attributes["KVP"] == 120.0
    assert info.warnings == []


def test_skips_non_dicom_files(small_series: Path, tmp_path: Path) -> None:
    target = shutil.copytree(small_series, tmp_path / "with_junk")
    (target / "notes.txt").write_text("not dicom")
    (target / ".DS_Store").write_bytes(b"\x00\x01")
    info = read_series_headers(target)
    assert info.num_slices == 40
    assert info.skipped_files == 1  # hidden files are ignored entirely
    assert any("Skipped 1 file" in w for w in info.warnings)


def test_rejects_two_series_in_one_directory(small_series: Path, tmp_path: Path) -> None:
    target = shutil.copytree(small_series, tmp_path / "mixed")
    write_series(target, slices=5, size=16)
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(target)
    assert err.value.code == "multiple_series"


def test_rejects_directory_without_dicom(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(tmp_path)
    assert err.value.code == "no_dicom_images"


def test_rejects_empty_and_missing_directories(tmp_path: Path) -> None:
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(tmp_path)
    assert err.value.code == "empty_directory"
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(tmp_path / "nope")
    assert err.value.code == "path_not_found"


def test_rejects_duplicate_positions(small_series: Path, tmp_path: Path) -> None:
    target = shutil.copytree(small_series, tmp_path / "dupes")
    import pydicom
    from pydicom.uid import generate_uid

    src = sorted(target.glob("*.dcm"))[0]
    ds = pydicom.dcmread(src)
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.save_as(target / "copy.dcm")
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(target)
    assert err.value.code == "duplicate_positions"


def test_warns_about_gaps(tmp_path: Path) -> None:
    path = write_series(tmp_path / "gappy", slices=20, size=16, slice_spacing=2.0, drop_slices=(10, 11))
    info = read_series_headers(path)
    assert info.num_slices == 18
    assert info.computed_slice_spacing == pytest.approx(2.0)
    assert any("gap" in w for w in info.warnings)
    assert np.isclose(max(np.diff([s.z for s in info.slices])), 6.0)


def _rewrite(directory: Path, change) -> None:
    import pydicom

    for i, f in enumerate(sorted(directory.glob("*.dcm"), key=lambda p: float(pydicom.dcmread(p).SliceLocation))):
        ds = pydicom.dcmread(f)
        change(ds, i)
        ds.save_as(f)


@pytest.mark.parametrize(
    "orientation",
    [
        [0, 1, 0, -1, 0, 0],  # rotated 90° in plane: the "sagittal" column would really be coronal
        [1, 0, 0, 0, 0.97, 0.243],  # ~14° gantry tilt: still roughly axial, but sheared
    ],
)
def test_rejects_rotated_or_tilted_planes(small_series: Path, tmp_path: Path, orientation) -> None:
    target = shutil.copytree(small_series, tmp_path / "rotated")
    _rewrite(target, lambda ds, i: setattr(ds, "ImageOrientationPatient", orientation))
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(target)
    assert err.value.code == "unsupported_orientation"


def test_accepts_flipped_axial_planes(small_series: Path, tmp_path: Path) -> None:
    target = shutil.copytree(small_series, tmp_path / "flipped")
    _rewrite(target, lambda ds, i: setattr(ds, "ImageOrientationPatient", [-1, 0, 0, 0, -1, 0]))
    assert read_series_headers(target).num_slices == 40


@pytest.mark.parametrize("spacing", [[0, 1], [-1, 1], [float("nan"), 1], [float("inf"), 1], [1]])
def test_rejects_invalid_pixel_spacing(small_series: Path, tmp_path: Path, spacing) -> None:
    target = shutil.copytree(small_series, tmp_path / "invalid_spacing")
    _rewrite(target, lambda ds, i: setattr(ds, "PixelSpacing", spacing))
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(target)
    assert err.value.code == "invalid_pixel_spacing"


@pytest.mark.parametrize("missing", [False, True])
def test_rejects_inconsistent_pixel_spacing(small_series: Path, tmp_path: Path, missing) -> None:
    target = shutil.copytree(small_series, tmp_path / "mixed_spacing")

    def change(ds, i):
        if i == 5:
            if missing:
                del ds.PixelSpacing
            else:
                ds.PixelSpacing = [1, 2]

    _rewrite(target, change)
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(target)
    assert err.value.code == "inconsistent_geometry"


def test_rejects_drifting_slice_origins(small_series: Path, tmp_path: Path) -> None:
    target = shutil.copytree(small_series, tmp_path / "drift")

    def shift(ds, i):
        x, y, z = (float(v) for v in ds.ImagePositionPatient)
        ds.ImagePositionPatient = [x, y + 0.5 * i, z]  # 20 mm over 40 slices

    _rewrite(target, shift)
    with pytest.raises(SeriesValidationError) as err:
        read_series_headers(target)
    assert err.value.code == "position_drift"


def test_nonstandard_uid_is_a_warning_not_a_path(small_series: Path, tmp_path: Path) -> None:
    target = shutil.copytree(small_series, tmp_path / "bad_uid")
    _rewrite(target, lambda ds, i: setattr(ds, "SeriesInstanceUID", "../../outside"))
    info = read_series_headers(target)
    assert any("not a standard DICOM UID" in w for w in info.warnings)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_subdirectory_fails_instead_of_shrinking_the_series(small_series: Path, tmp_path: Path) -> None:
    target = shutil.copytree(small_series, tmp_path / "locked")
    locked = target / "more"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            read_series_headers(target)
    finally:
        locked.chmod(0o755)
