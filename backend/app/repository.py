"""SQL for the metadata index. Plain SQL with psycopg; no ORM."""

from __future__ import annotations

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.dicom.series import SeriesInfo

SERIES_SELECT = """
    SELECT s.id, s.series_instance_uid, s.status, s.source_path, s.modality, s.series_description,
           s.series_number, s.rows, s.columns, s.num_slices, s.pixel_spacing_row, s.pixel_spacing_col,
           s.slice_thickness, s.spacing_between_slices, s.computed_slice_spacing, s.z_min, s.z_max,
           s.image_orientation, s.attributes, s.warnings, s.created_at, s.updated_at,
           st.id AS study_id, st.study_instance_uid, st.patient_id, st.study_description, st.study_date
    FROM series s
    JOIN studies st ON st.id = s.study_id
"""


def upsert_series(conn: Connection, info: SeriesInfo, source_path: str) -> int:
    """Insert or refresh a series and its slices. Re-indexing the same series is idempotent."""
    study_id = conn.execute(
        """
        INSERT INTO studies (study_instance_uid, patient_id, study_description, study_date)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (study_instance_uid) DO UPDATE SET
            patient_id = EXCLUDED.patient_id,
            study_description = EXCLUDED.study_description,
            study_date = EXCLUDED.study_date,
            updated_at = now()
        RETURNING id
        """,
        (info.study_instance_uid, info.patient_id, info.study_description, info.study_date),
    ).fetchone()["id"]

    row_spacing, col_spacing = info.pixel_spacing or (None, None)
    series_id = conn.execute(
        """
        INSERT INTO series (series_instance_uid, study_id, status, source_path, modality, series_description,
                            series_number, rows, columns, num_slices, pixel_spacing_row, pixel_spacing_col,
                            slice_thickness, spacing_between_slices, computed_slice_spacing, z_min, z_max,
                            image_orientation, attributes, warnings)
        VALUES (%s, %s, 'indexed', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (series_instance_uid) DO UPDATE SET
            study_id = EXCLUDED.study_id, status = 'indexed', source_path = EXCLUDED.source_path,
            modality = EXCLUDED.modality, series_description = EXCLUDED.series_description,
            series_number = EXCLUDED.series_number, rows = EXCLUDED.rows, columns = EXCLUDED.columns,
            num_slices = EXCLUDED.num_slices, pixel_spacing_row = EXCLUDED.pixel_spacing_row,
            pixel_spacing_col = EXCLUDED.pixel_spacing_col, slice_thickness = EXCLUDED.slice_thickness,
            spacing_between_slices = EXCLUDED.spacing_between_slices,
            computed_slice_spacing = EXCLUDED.computed_slice_spacing, z_min = EXCLUDED.z_min,
            z_max = EXCLUDED.z_max, image_orientation = EXCLUDED.image_orientation,
            attributes = EXCLUDED.attributes, warnings = EXCLUDED.warnings, updated_at = now()
        RETURNING id
        """,
        (
            info.series_instance_uid,
            study_id,
            source_path,
            info.modality,
            info.series_description,
            info.series_number,
            info.rows,
            info.columns,
            info.num_slices,
            row_spacing,
            col_spacing,
            info.slice_thickness,
            info.spacing_between_slices,
            info.computed_slice_spacing,
            info.z_min,
            info.z_max,
            list(info.orientation),
            Jsonb(info.attributes),
            info.warnings,
        ),
    ).fetchone()["id"]

    conn.execute("DELETE FROM instances WHERE series_id = %s", (series_id,))
    conn.execute("DELETE FROM analysis_results WHERE series_id = %s", (series_id,))
    columns = "series_id, slice_index, sop_instance_uid, instance_number, z_position, file_path"
    with conn.cursor() as cur, cur.copy(f"COPY instances ({columns}) FROM STDIN") as copy:
        for index, s in enumerate(info.slices):
            copy.write_row((series_id, index, s.sop_instance_uid, s.instance_number, s.z, s.path))
    return series_id


def get_series(conn: Connection, series_id: int) -> dict | None:
    row = conn.execute(f"{SERIES_SELECT} WHERE s.id = %s", (series_id,)).fetchone()
    if row is None:
        return None
    row["analysis"] = conn.execute(
        "SELECT job_id, sagittal_path, sagittal_geometry, qc, created_at FROM analysis_results WHERE series_id = %s",
        (series_id,),
    ).fetchone()
    row["last_analysis_job"] = conn.execute(
        """
        SELECT id, status, error_code, error_message, attempts, max_attempts, finished_at
        FROM jobs WHERE series_id = %s AND kind = 'analyze' ORDER BY id DESC LIMIT 1
        """,
        (series_id,),
    ).fetchone()
    return row


def search_series(
    conn: Connection,
    *,
    q: str | None = None,
    study_description: str | None = None,
    max_slice_thickness: float | None = None,
    min_slice_thickness: float | None = None,
    modality: str | None = None,
    patient_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Cross-study queries, e.g. every series thinner than 1.5 mm, or every study with a given description."""
    clauses: list[str] = []
    params: list = []
    if q:
        clauses.append("(st.study_description ILIKE %s OR s.series_description ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if study_description:
        clauses.append("lower(st.study_description) = lower(%s)")
        params.append(study_description)
    if max_slice_thickness is not None:
        clauses.append("s.slice_thickness < %s")
        params.append(max_slice_thickness)
    if min_slice_thickness is not None:
        clauses.append("s.slice_thickness >= %s")
        params.append(min_slice_thickness)
    if modality:
        clauses.append("s.modality = %s")
        params.append(modality.upper())
    if patient_id:
        clauses.append("st.patient_id = %s")
        params.append(patient_id)
    if status:
        clauses.append("s.status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = conn.execute(
        f"SELECT count(*) AS n FROM series s JOIN studies st ON st.id = s.study_id {where}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"{SERIES_SELECT} {where} ORDER BY st.study_date DESC NULLS LAST, s.id DESC LIMIT %s OFFSET %s",
        (*params, limit, offset),
    ).fetchall()
    return rows, total


def slice_positions(conn: Connection, series_id: int) -> list[dict]:
    return conn.execute(
        "SELECT slice_index, z_position, instance_number FROM instances WHERE series_id = %s ORDER BY slice_index",
        (series_id,),
    ).fetchall()


def get_instance(conn: Connection, series_id: int, slice_index: int) -> dict | None:
    return conn.execute(
        "SELECT slice_index, z_position, instance_number, file_path FROM instances "
        "WHERE series_id = %s AND slice_index = %s",
        (series_id, slice_index),
    ).fetchone()


def list_instances(conn: Connection, series_id: int) -> list[dict]:
    return conn.execute(
        "SELECT slice_index, z_position, file_path FROM instances WHERE series_id = %s ORDER BY slice_index",
        (series_id,),
    ).fetchall()


def save_analysis(conn: Connection, series_id: int, job_id: int, sagittal_path: str, geometry: dict, qc: dict) -> None:
    conn.execute(
        """
        INSERT INTO analysis_results (series_id, job_id, sagittal_path, sagittal_geometry, qc)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (series_id) DO UPDATE SET job_id = EXCLUDED.job_id, sagittal_path = EXCLUDED.sagittal_path,
            sagittal_geometry = EXCLUDED.sagittal_geometry, qc = EXCLUDED.qc, created_at = now()
        """,
        (series_id, job_id, sagittal_path, Jsonb(geometry), Jsonb(qc)),
    )
    set_series_status(conn, series_id, "ready")


def set_series_status(conn: Connection, series_id: int, status: str) -> None:
    conn.execute("UPDATE series SET status = %s, updated_at = now() WHERE id = %s", (status, series_id))


def indexed_source_paths(conn: Connection) -> dict[str, int]:
    return {r["source_path"]: r["id"] for r in conn.execute("SELECT id, source_path FROM series").fetchall()}
