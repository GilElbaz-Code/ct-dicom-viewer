from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.dicom.analysis import analyze_series
from app.dicom.imaging import (
    apply_window,
    closest_index,
    encode_png,
    load_slice_hu,
    resample_sagittal,
    to_display_orientation,
)
from app.dicom.series import read_series_headers


@pytest.mark.parametrize(
    ("z", "expected"),
    [(-100.0, 0), (0.0, 0), (1.2, 0), (1.3, 1), (2.5, 1), (3.9, 2), (5.0, 2), (99.0, 2)],
)
def test_closest_index(z: float, expected: int) -> None:
    assert closest_index([0.0, 2.5, 5.0], z) == expected


def test_window_soft_tissue_defaults() -> None:
    hu = np.array([-1000, -160, 40, 240, 1000], dtype=np.float32)
    out = apply_window(hu, width=400, level=40)
    assert out.tolist() == [0, 0, 127, 255, 255]


def test_window_rejects_non_positive_width() -> None:
    with pytest.raises(ValueError):
        apply_window(np.zeros(3), width=0, level=0)


def test_png_roundtrip() -> None:
    img = (np.arange(64 * 32) % 256).astype(np.uint8).reshape(64, 32)
    decoded = np.array(Image.open(io.BytesIO(encode_png(img))))
    assert decoded.shape == (64, 32) and (decoded == img).all()


def test_display_orientation_flips_negative_cosines() -> None:
    img = np.arange(6).reshape(2, 3)
    assert (to_display_orientation(img, [1, 0, 0, 0, 1, 0]) == img).all()
    assert (to_display_orientation(img, [-1, 0, 0, 0, 1, 0]) == img[:, ::-1]).all()
    assert (to_display_orientation(img, [1, 0, 0, 0, -1, 0]) == img[::-1, :]).all()


def test_slice_is_converted_to_hounsfield(small_series: Path) -> None:
    info = read_series_headers(small_series)
    hu = load_slice_hu(info.slices[20].path, info.orientation)
    assert hu.dtype == np.float32
    assert hu[0, 0] == pytest.approx(-1000)  # air in the corner, stored as 24 with intercept -1024
    assert -50 < hu[32, 32] < 150  # soft tissue near the centre


def test_sagittal_has_physical_proportions_and_superior_on_top() -> None:
    z = np.array([0.0, 2.0, 4.0, 6.0])
    columns = np.array([[0, 0], [10, 10], [20, 20], [30, 30]], dtype=np.float32)
    plane, geom = resample_sagittal(columns, z, mm_per_pixel_y=0.5, column_index=1)
    assert plane.shape == (13, 2)  # 6 mm of z at 0.5 mm per row
    assert plane[0, 0] == 30 and plane[-1, 0] == 0  # top row is the highest z
    assert plane[1, 0] == 28  # interpolated 0.5 mm below the top slice
    assert geom.row_fraction(6.0) == 0 and geom.row_fraction(0.0) == 1 and geom.row_fraction(3.0) == 0.5


def test_sagittal_interpolates_across_gaps() -> None:
    z = np.array([0.0, 1.0, 5.0])  # missing slices between 1 and 5 mm
    columns = np.array([[0.0], [10.0], [50.0]], dtype=np.float32)
    plane, geom = resample_sagittal(columns, z, mm_per_pixel_y=1.0, column_index=0)
    assert plane[:, 0].tolist() == [50, 40, 30, 20, 10, 0]  # z = 5,4,3,2,1,0
    assert geom.height == 6


def test_analysis_streams_the_volume(small_series: Path) -> None:
    info = read_series_headers(small_series)
    out = analyze_series(
        [s.path for s in info.slices],
        [s.z for s in info.slices],
        rows=info.rows,
        columns=info.columns,
        orientation=info.orientation,
        pixel_spacing=info.pixel_spacing,
        slice_spacing=info.computed_slice_spacing,
    )
    assert out.sagittal.dtype == np.int16
    assert out.sagittal.shape == (round(97.5 / 5.0) + 1, 64)
    assert out.qc["hu_min"] == pytest.approx(-1000, abs=1)
    assert out.qc["hu_p01"] == -1000
    assert out.qc["slice_gaps"] == 0
    assert out.qc["body_volume_ml_estimate"] > 0


def test_sagittal_clips_instead_of_wrapping_int16() -> None:
    columns = np.array([[40000.0], [-40000.0]], dtype=np.float32)
    plane, _ = resample_sagittal(columns, np.array([0.0, 1.0]), mm_per_pixel_y=1.0, column_index=0)
    assert plane[:, 0].tolist() == [-32768, 32767]
