"""Where DICOM input may come from: validated paths and CSV batch files."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.errors import SeriesValidationError

PATH_HEADERS = {"path", "dicom_path", "dicom_dir", "directory", "dir", "series_path"}


def resolve_source_path(raw: str, settings: Settings) -> Path:
    """Turn user input into a real directory inside an allowed root.

    Relative paths are taken relative to the library root. Symlinks and `..`
    are resolved before the check, so `../../etc` cannot escape the roots.
    """
    text = (raw or "").strip()
    if not text:
        raise SeriesValidationError("invalid_path", "Path is empty")
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = settings.library_root / candidate
    resolved = candidate.resolve()
    roots = [root.resolve() for root in settings.source_roots]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        allowed = ", ".join(str(r) for r in settings.source_roots)
        raise SeriesValidationError("path_not_allowed", f"{text} is outside the data roots ({allowed})")
    if not resolved.exists():
        raise SeriesValidationError("path_not_found", f"Directory does not exist: {text}")
    if not resolved.is_dir():
        raise SeriesValidationError("not_a_directory", f"Not a directory: {text}")
    return resolved


@dataclass(frozen=True)
class CsvRow:
    row_number: int  # 1-based data row, as a user would count it in a spreadsheet
    path: str


def parse_batch_csv(content: bytes, max_rows: int) -> list[CsvRow]:
    """Read DICOM directory paths from a CSV.

    Accepts a single column without a header, or any number of columns with a
    header row where one column is named path / dicom_path / dicom_dir /
    directory. Blank lines and lines starting with '#' are ignored.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 text") from exc

    rows = [r for r in csv.reader(io.StringIO(text)) if r and any(c.strip() for c in r)]
    rows = [r for r in rows if not r[0].lstrip().startswith("#")]
    if not rows:
        raise ValueError("CSV has no rows")

    header = [c.strip().lower() for c in rows[0]]
    column = 0
    if any(h in PATH_HEADERS for h in header):
        column = next(i for i, h in enumerate(header) if h in PATH_HEADERS)
        rows = rows[1:]
    elif len(header) > 1:
        raise ValueError(f"CSV has {len(header)} columns; add a header row with a 'path' column")

    if len(rows) > max_rows:
        raise ValueError(f"CSV has {len(rows)} rows; the limit is {max_rows}")
    if not rows:
        raise ValueError("CSV has a header but no data rows")
    return [CsvRow(i, (r[column] if column < len(r) else "").strip()) for i, r in enumerate(rows, start=1)]
