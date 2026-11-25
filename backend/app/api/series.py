"""Querying the metadata index and serving the viewer images."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Response

from app import repository
from app.api.deps import Conn
from app.dicom.imaging import apply_window, closest_index, encode_png, load_slice_hu

router = APIRouter(prefix="/series", tags=["series"])

Window = Annotated[float, Query(gt=0, le=10000, description="Window width in HU")]
Level = Annotated[float, Query(ge=-2000, le=5000, description="Window level (centre) in HU")]
IMAGE_HEADERS = {"Cache-Control": "private, max-age=300"}


def _series_or_404(conn: Conn, series_id: int) -> dict:
    series = repository.get_series(conn, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    return series


@router.get("")
def search_series(
    conn: Conn,
    q: Annotated[str | None, Query(description="Text in the study or series description")] = None,
    study_description: Annotated[str | None, Query(description="Exact study description (case-insensitive)")] = None,
    max_slice_thickness: Annotated[float | None, Query(gt=0, description="Slice thickness below this, mm")] = None,
    min_slice_thickness: Annotated[float | None, Query(gt=0)] = None,
    modality: str | None = None,
    patient_id: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    items, total = repository.search_series(
        conn,
        q=q,
        study_description=study_description,
        max_slice_thickness=max_slice_thickness,
        min_slice_thickness=min_slice_thickness,
        modality=modality,
        patient_id=patient_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total}


@router.get("/{series_id}")
def get_series(series_id: int, conn: Conn) -> dict:
    return _series_or_404(conn, series_id)


@router.get("/{series_id}/locate")
def locate_slice(
    series_id: int,
    conn: Conn,
    z: Annotated[
        float | None,
        Query(allow_inf_nan=False, description="Location along the patient axis, mm (ImagePositionPatient z)"),
    ] = None,
    index: Annotated[int | None, Query(description="Slice index, 0 = most inferior")] = None,
) -> dict:
    """Resolve a location in mm, or a slice index, to the slice to display."""
    if (z is None) == (index is None):
        raise HTTPException(422, "Give exactly one of z or index")
    series = _series_or_404(conn, series_id)
    slices = repository.slice_positions(conn, series_id)
    positions = [s["z_position"] for s in slices]
    if index is not None:
        if not 0 <= index < len(slices):
            raise HTTPException(422, f"index must be between 0 and {len(slices) - 1}")
        chosen = index
    else:
        chosen = closest_index(positions, z)
    hit = slices[chosen]
    half_step = series["computed_slice_spacing"] / 2
    span = series["z_max"] - series["z_min"]
    return {
        "index": chosen,
        "z": hit["z_position"],
        "instance_number": hit["instance_number"],
        "num_slices": len(slices),
        "requested_z": z,
        "distance_mm": abs(hit["z_position"] - z) if z is not None else 0.0,
        "outside_volume": z is not None and not (series["z_min"] - half_step <= z <= series["z_max"] + half_step),
        # where to draw the line on the sagittal image: 0 = top (z_max), 1 = bottom (z_min)
        "sagittal_fraction": (series["z_max"] - hit["z_position"]) / span if span else 0.0,
    }


@router.get("/{series_id}/axial/{index}", response_class=Response, responses={200: {"content": {"image/png": {}}}})
def axial_image(series_id: int, index: int, conn: Conn, window: Window = 400, level: Level = 40) -> Response:
    series = _series_or_404(conn, series_id)
    instance = repository.get_instance(conn, series_id, index)
    if instance is None:
        raise HTTPException(404, f"Slice {index} not found (series has {series['num_slices']})")
    try:
        hu = load_slice_hu(instance["file_path"], series["image_orientation"])
    except FileNotFoundError:
        raise HTTPException(410, f"Source file is gone: {instance['file_path']}") from None
    except (ValueError, NotImplementedError, RuntimeError, AttributeError) as exc:
        raise HTTPException(422, f"Could not decode pixel data of slice {index}: {exc}") from None
    return Response(encode_png(apply_window(hu, window, level)), media_type="image/png", headers=IMAGE_HEADERS)


@lru_cache(maxsize=32)
def _load_sagittal(path: str, mtime_ns: int) -> np.ndarray:  # mtime in the key: re-analysis invalidates
    return np.load(path)


@router.get("/{series_id}/sagittal", response_class=Response, responses={200: {"content": {"image/png": {}}}})
def sagittal_image(series_id: int, conn: Conn, window: Window = 400, level: Level = 40) -> Response:
    series = _series_or_404(conn, series_id)
    analysis = series["analysis"]
    if analysis is None:
        job = series["last_analysis_job"]
        if job and job["status"] == "failed":
            raise HTTPException(409, f"Analysis failed: [{job['error_code']}] {job['error_message']}")
        raise HTTPException(409, "Sagittal view is not ready yet; the analysis job is still pending")
    path = Path(analysis["sagittal_path"])
    if not path.exists():
        raise HTTPException(410, "Analysis output is missing; retry the analysis job")
    plane = _load_sagittal(str(path), path.stat().st_mtime_ns)
    return Response(encode_png(apply_window(plane, window, level)), media_type="image/png", headers=IMAGE_HEADERS)
