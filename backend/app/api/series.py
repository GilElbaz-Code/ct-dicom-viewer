"""HTTP routes for querying series and serving the viewer images."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from app.api.schemas import INVALID, NOT_FOUND, PNG, BeforeId, Limit, Located, Series
from app.services import viewer
from app.types import LocatedRecord, SeriesRecord

router = APIRouter(prefix="/api/series", tags=["series"])

Width = Annotated[float, Query(gt=0, le=10000, description="Window width (HU)")]
Level = Annotated[float, Query(ge=-2000, le=5000, description="Window level, the centre (HU)")]
SeriesId = Annotated[int, Path(ge=1, le=2**63 - 1)]  # bigint
SliceIndex = Annotated[int, Path(ge=0, lt=2**31 - 1)]  # PostgreSQL array subscripts are int4


@router.get("", status_code=HTTPStatus.OK, response_model=list[Series], responses=INVALID)
def search(
    max_slice_thickness: Annotated[float | None, Query(gt=0, description="Only series thinner than this (mm)")] = None,
    description: Annotated[str | None, Query(description="Text in the study or series description")] = None,
    before_id: BeforeId = None,
    limit: Limit = 100,
) -> list[SeriesRecord]:
    return viewer.search(
        max_slice_thickness=max_slice_thickness, description=description, before_id=before_id, limit=limit
    )


@router.get("/{series_id}", status_code=HTTPStatus.OK, response_model=Series, responses={**NOT_FOUND, **INVALID})
def get_series(series_id: SeriesId) -> SeriesRecord:
    return viewer.get_series(series_id=series_id)


@router.get(
    "/{series_id}/locate", status_code=HTTPStatus.OK, response_model=Located, responses={**NOT_FOUND, **INVALID}
)
def locate(
    series_id: SeriesId,
    z: Annotated[float | None, Query(allow_inf_nan=False, description="Location in mm (IPP z)")] = None,
    index: Annotated[int | None, Query(description="Slice index, 0 = most inferior")] = None,
) -> LocatedRecord:
    return viewer.locate_in_series(series_id=series_id, z=z, index=index)


@router.get(
    "/{series_id}/axial/{index}",
    status_code=HTTPStatus.OK,
    response_class=Response,
    responses={**PNG, **NOT_FOUND, **INVALID},
)
def axial(series_id: SeriesId, index: SliceIndex, window: Width = 400, level: Level = 40) -> Response:
    return Response(
        content=viewer.axial_png(series_id=series_id, index=index, width=window, level=level), media_type="image/png"
    )


@router.get(
    "/{series_id}/sagittal",
    status_code=HTTPStatus.OK,
    response_class=Response,
    responses={**PNG, **NOT_FOUND, **INVALID},
)
def sagittal(series_id: SeriesId, window: Width = 400, level: Level = 40) -> Response:
    return Response(content=viewer.sagittal_png(series_id=series_id, width=window, level=level), media_type="image/png")
