from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import SeriesValidationError
from app.sources import parse_batch_csv, resolve_source_path


def test_csv_single_column_without_header() -> None:
    rows = parse_batch_csv(b"series_a\n\n# a comment\nseries_b\n", max_rows=10)
    assert [(r.row_number, r.path) for r in rows] == [(1, "series_a"), (2, "series_b")]


def test_csv_with_header_and_extra_columns() -> None:
    content = "﻿subject,dicom_dir,note\n1,/data/library/a,x\n2,b,\n".encode()
    rows = parse_batch_csv(content, max_rows=10)
    assert [r.path for r in rows] == ["/data/library/a", "b"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "no rows"),
        (b"path\n", "no data rows"),
        (b"a,b\nc,d\n", "header row"),
        (b"\xff\xfe\x00", "UTF-8"),
        (b"a\nb\nc\n", "limit"),
    ],
)
def test_csv_errors(content: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_batch_csv(content, max_rows=2)


def test_relative_paths_resolve_under_library(data_roots: Settings) -> None:
    (data_roots.library_root / "series_1").mkdir()
    assert resolve_source_path("series_1", data_roots) == (data_roots.library_root / "series_1").resolve()


def test_absolute_path_in_upload_root_is_allowed(data_roots: Settings) -> None:
    target = data_roots.upload_root / "abc"
    target.mkdir()
    assert resolve_source_path(str(target), data_roots) == target.resolve()


@pytest.mark.parametrize("raw", ["../../etc", "/etc", "series_1/../../.."])
def test_paths_cannot_escape_the_data_roots(data_roots: Settings, raw: str) -> None:
    (data_roots.library_root / "series_1").mkdir()
    with pytest.raises(SeriesValidationError) as err:
        resolve_source_path(raw, data_roots)
    assert err.value.code == "path_not_allowed"


def test_symlink_out_of_root_is_rejected(data_roots: Settings, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_roots.library_root / "link").symlink_to(outside)
    with pytest.raises(SeriesValidationError) as err:
        resolve_source_path("link", data_roots)
    assert err.value.code == "path_not_allowed"


def test_missing_path_and_file_path(data_roots: Settings) -> None:
    with pytest.raises(SeriesValidationError) as err:
        resolve_source_path("missing", data_roots)
    assert err.value.code == "path_not_found"
    (data_roots.library_root / "file.dcm").write_text("x")
    with pytest.raises(SeriesValidationError) as err:
        resolve_source_path("file.dcm", data_roots)
    assert err.value.code == "not_a_directory"
