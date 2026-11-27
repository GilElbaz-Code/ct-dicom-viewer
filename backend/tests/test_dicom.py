import io
from pathlib import Path

import numpy as np
from PIL import Image

from app.services import dicom
from tests.conftest import write_slice


def test_reads_headers_and_sorts_by_position_not_file_name(make_series):
    info = dicom.read_series(make_series(n=5, step=2.5))
    assert info.z_positions == [-100, -97.5, -95, -92.5, -90]
    assert [Path(f).name for f in info.files] == ["005.dcm", "004.dcm", "003.dcm", "002.dcm", "001.dcm"]
    assert info.slice_spacing == 2.5
    assert info.spacing_between_slices is None  # tag absent: the UI shows the measured spacing instead
    assert (info.slice_thickness, info.pixel_spacing, info.rows, info.modality) == (1.0, [0.5, 0.5], 8, "CT")


def test_sagittal_keeps_the_middle_column_of_each_slice_in_z_order(tmp_path):
    for i in range(3):  # file names run opposite to z; stored pixels are 100 * column + slice, so columns differ
        write_slice(tmp_path / f"{3 - i}.dcm", z=-100 + 2.5 * i, value=100 * np.arange(8) + i)
    info = dicom.read_series(tmp_path)
    plane = dicom.sagittal(info.files, info.rows, info.columns)
    assert plane.shape == (3, 8)
    # Column 8 // 2 = 4 of every slice, lowest z first: HU = 100 * 4 + i - 1024.
    assert plane.tolist() == [[-624 + i] * 8 for i in range(3)]


def test_window_maps_level_minus_and_plus_half_width_to_black_and_white():
    png = dicom.window_png(np.array([[-160.0, 40.0, 240.0, 1000.0]]), width=400, level=40)
    assert np.array(Image.open(io.BytesIO(png))).tolist() == [[0, 127, 255, 255]]
