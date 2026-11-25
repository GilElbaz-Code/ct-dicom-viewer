"""Querying the metadata index and serving the viewer images."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request, Response

from app import repository
from app.api.deps import Conn
from app.api.schemas import Located, SeriesDetail, SeriesPage, SeriesStatus
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


@router.get("", response_model=SeriesPage)
def search_series(
    conn: Conn,
    q: Annotated[str | None, Query(description="Text in the study or series description")] = None,
    study_description: Annotated[str | None, Query(description="Exact study description (case-insensitive)")] = None,
    max_slice_thickness: Annotated[float | None, Query(gt=0, description="Slice thickness below this, mm")] = None,
    min_slice_thickness: Annotated[float | None, Query(gt=0)] = None,
    modality: str | None = None,
    patient_id: str | None = None,
    status: SeriesStatus | None = None,
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


@router.get("/{series_id}", response_model=SeriesDetail)
def get_series(series_id: int, conn: Conn) -> dict:
    # The metadata and positions must describe the same version during a concurrent re-index.
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    series = _series_or_404(conn, series_id)
    series["slices"] = repository.slice_positions(conn, series_id)
    return series


@router.get("/{series_id}/locate", response_model=Located)
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
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
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
        "sagittal_fraction": (series["z_max"] - hit["z_position"]) / span if span else 0.0,
    }


@router.get("/{series_id}/axial/{index}", response_class=Response, responses={200: {"content": {"image/png": {}}}})
def axial_image(series_id: int, index: int, request: Request, window: Window = 400, level: Level = 40) -> Response:
    with request.app.state.pool.connection() as conn:
        instance = conn.execute(
            "SELECT i.file_path, s.image_orientation FROM instances i JOIN series s ON s.id = i.series_id "
            "WHERE i.series_id = %s AND i.slice_index = %s",
            (series_id, index),
        ).fetchone()
    if instance is None:
        raise HTTPException(404, "Series or slice not found")
    try:
        hu = load_slice_hu(instance["file_path"], instance["image_orientation"])
    except FileNotFoundError:
        raise HTTPException(410, f"Source file is gone: {instance['file_path']}") from None
    except (ValueError, NotImplementedError, RuntimeError, AttributeError) as exc:
        raise HTTPException(422, f"Could not decode pixel data of slice {index}: {exc}") from None
    return Response(encode_png(apply_window(hu, window, level)), media_type="image/png", headers=IMAGE_HEADERS)


@lru_cache(maxsize=32)
def _load_sagittal(path: str, mtime_ns: int) -> np.ndarray:  # mtime in the key: re-analysis invalidates
    return np.load(path)


@router.get("/{series_id}/sagittal", response_class=Response, responses={200: {"content": {"image/png": {}}}})
def sagittal_image(series_id: int, request: Request, window: Window = 400, level: Level = 40) -> Response:
    with request.app.state.pool.connection() as conn:
        series = conn.execute(
            "SELECT s.id, a.sagittal_path FROM series s LEFT JOIN analysis_results a ON a.series_id = s.id "
            "WHERE s.id = %s",
            (series_id,),
        ).fetchone()
        if series is None:
            raise HTTPException(404, "Series not found")
        if series["sagittal_path"] is None:
            job = conn.execute(
                "SELECT status, error_code, error_message FROM jobs WHERE series_id = %s AND kind = 'analyze' "
                "ORDER BY id DESC LIMIT 1",
                (series_id,),
            ).fetchone()
            if job and job["status"] == "failed":
                raise HTTPException(409, f"Analysis failed: [{job['error_code']}] {job['error_message']}")
            raise HTTPException(409, "Sagittal view is not ready yet; the analysis job is still pending")
    path = Path(series["sagittal_path"])
    if not path.exists():
        raise HTTPException(410, "Analysis output is missing; retry the analysis job")
    plane = _load_sagittal(str(path), path.stat().st_mtime_ns)
    return Response(encode_png(apply_window(plane, window, level)), media_type="image/png", headers=IMAGE_HEADERS)
