from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.ollama import VLM_FIELDS


def preferred_mapillary_geometry(
    geometry: dict[str, Any] | None,
    image_properties: dict[str, Any] | None,
) -> dict[str, Any] | None:
    properties = image_properties or {}
    return (
        properties.get("mapmatched_geometry")
        or properties.get("mapmatching_geometry")
        or properties.get("computed_geometry")
        or properties.get("mapillary_geometry")
        or properties.get("original_geometry")
        or properties.get("gps_geometry")
        or geometry
    )


@dataclass(frozen=True)
class VLMAnalysisStore:
    database_url: str | None

    @property
    def configured(self) -> bool:
        return bool(self.database_url)

    def ensure_schema(self) -> None:
        if not self.database_url:
            return
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vlm_image_analysis (
                    image_id text PRIMARY KEY,
                    grid_id text NOT NULL,
                    model text NOT NULL,
                    prompt_version text NOT NULL,
                    geometry jsonb,
                    image_properties jsonb,
                    fields jsonb NOT NULL,
                    raw_response jsonb,
                    error text,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                """
                ALTER TABLE vlm_image_analysis
                ADD COLUMN IF NOT EXISTS geometry jsonb
                """
            )
            conn.execute(
                """
                ALTER TABLE vlm_image_analysis
                ADD COLUMN IF NOT EXISTS image_properties jsonb
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS vlm_image_analysis_grid_idx
                ON vlm_image_analysis (grid_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vlm_processing_jobs (
                    job_id text PRIMARY KEY,
                    grid_id text NOT NULL,
                    model text NOT NULL,
                    force boolean NOT NULL DEFAULT false,
                    status text NOT NULL,
                    total integer NOT NULL DEFAULT 0,
                    processed integer NOT NULL DEFAULT 0,
                    analyzed integer NOT NULL DEFAULT 0,
                    skipped integer NOT NULL DEFAULT 0,
                    failed integer NOT NULL DEFAULT 0,
                    current_image_id text,
                    last_stored_image_id text,
                    analysis_seconds_sum double precision NOT NULL DEFAULT 0,
                    error text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    started_at timestamptz,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    completed_at timestamptz
                )
                """
            )
            conn.execute(
                """
                ALTER TABLE vlm_processing_jobs
                ADD COLUMN IF NOT EXISTS analysis_seconds_sum double precision NOT NULL DEFAULT 0
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vlm_processing_job_items (
                    job_id text NOT NULL REFERENCES vlm_processing_jobs(job_id) ON DELETE CASCADE,
                    image_id text NOT NULL,
                    position integer NOT NULL,
                    image jsonb NOT NULL,
                    status text NOT NULL DEFAULT 'queued',
                    error text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (job_id, image_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS vlm_processing_jobs_status_created_idx
                ON vlm_processing_jobs (status, created_at)
                """
            )

    def create_job(
        self,
        job_id: str,
        grid_id: str,
        model: str,
        force: bool,
        images: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self.ensure_schema()
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            job = conn.execute(
                """
                INSERT INTO vlm_processing_jobs
                    (job_id, grid_id, model, force, status, total, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s)
                RETURNING *
                """,
                (job_id, grid_id, model, force, len(images), now, now),
            ).fetchone()
            for position, image in enumerate(images):
                image_id = _image_id(image)
                conn.execute(
                    """
                    INSERT INTO vlm_processing_job_items
                        (job_id, image_id, position, image, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, 'queued', %s, %s)
                    ON CONFLICT (job_id, image_id) DO UPDATE SET
                        image = EXCLUDED.image,
                        position = EXCLUDED.position,
                        status = 'queued',
                        error = NULL,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        job_id,
                        image_id,
                        position,
                        json.dumps(image, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
        return self._format_job_row(job)

    def recover_interrupted_jobs(self) -> int:
        if not self.database_url:
            return 0
        self.ensure_schema()
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE vlm_processing_jobs
                SET status = 'queued',
                    current_image_id = NULL,
                    updated_at = %s
                WHERE status = 'running'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE vlm_processing_job_items
                SET status = 'queued',
                    updated_at = %s
                WHERE status = 'running'
                """,
                (now,),
            )
        return int(result.rowcount or 0)

    def migrate_geometry_to_mapillary_computed(self) -> int:
        if not self.database_url:
            return 0
        self.ensure_schema()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE vlm_image_analysis
                SET geometry = COALESCE(
                        image_properties->'mapmatched_geometry',
                        image_properties->'mapmatching_geometry',
                        image_properties->'computed_geometry',
                        image_properties->'mapillary_geometry',
                        image_properties->'original_geometry',
                        image_properties->'gps_geometry',
                        geometry
                    )
                WHERE geometry IS DISTINCT FROM COALESCE(
                        image_properties->'mapmatched_geometry',
                        image_properties->'mapmatching_geometry',
                        image_properties->'computed_geometry',
                        image_properties->'mapillary_geometry',
                        image_properties->'original_geometry',
                        image_properties->'gps_geometry',
                        geometry
                    )
                """
            )
        return int(result.rowcount or 0)

    def claim_next_job(self) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        self.ensure_schema()
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            job = conn.execute(
                """
                SELECT *
                FROM vlm_processing_jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            ).fetchone()
            if job is None:
                return None
            job = conn.execute(
                """
                UPDATE vlm_processing_jobs
                SET status = 'running',
                    started_at = COALESCE(started_at, %s),
                    updated_at = %s
                WHERE job_id = %s
                RETURNING *
                """,
                (now, now, job["job_id"]),
            ).fetchone()
            items = conn.execute(
                """
                SELECT image_id, image
                FROM vlm_processing_job_items
                WHERE job_id = %s
                ORDER BY position ASC
                """,
                (job["job_id"],),
            ).fetchall()
        formatted = self._format_job_row(job)
        formatted["items"] = [
            {"image_id": row["image_id"], "image": row["image"]} for row in items
        ]
        return formatted

    def job(self, job_id: str) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vlm_processing_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        return self._format_job_row(row) if row else None

    def list_jobs(self, limit: int = 20) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "jobs": []}
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM vlm_processing_jobs
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return {
            "available": True,
            "count": len(rows),
            "jobs": [self._format_job_row(row) for row in rows],
        }

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        allowed = {
            "status",
            "processed",
            "analyzed",
            "skipped",
            "failed",
            "current_image_id",
            "last_stored_image_id",
            "analysis_seconds_sum",
            "error",
            "completed_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        updates["updated_at"] = datetime.now(timezone.utc)
        assignments = ", ".join(f"{key} = %s" for key in updates)
        values = list(updates.values()) + [job_id]
        with self._connect() as conn:
            row = conn.execute(
                f"""
                UPDATE vlm_processing_jobs
                SET {assignments}
                WHERE job_id = %s
                RETURNING *
                """,
                values,
            ).fetchone()
        return self._format_job_row(row)

    def update_job_item(
        self,
        job_id: str,
        image_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        if not self.database_url:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE vlm_processing_job_items
                SET status = %s,
                    error = %s,
                    updated_at = %s
                WHERE job_id = %s AND image_id = %s
                """,
                (status, error, datetime.now(timezone.utc), job_id, image_id),
            )

    def should_cancel_job(self, job_id: str) -> bool:
        if not self.database_url:
            return False
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM vlm_processing_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        return bool(row and row["status"] == "cancelling")

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self.ensure_schema()
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status
                FROM vlm_processing_jobs
                WHERE job_id = %s
                FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            status = row["status"]
            if status == "queued":
                updated = conn.execute(
                    """
                    UPDATE vlm_processing_jobs
                    SET status = 'cancelled',
                        current_image_id = NULL,
                        completed_at = %s,
                        updated_at = %s
                    WHERE job_id = %s
                    RETURNING *
                    """,
                    (now, now, job_id),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE vlm_processing_job_items
                    SET status = 'cancelled',
                        updated_at = %s
                    WHERE job_id = %s AND status = 'queued'
                    """,
                    (now, job_id),
                )
            elif status == "running":
                updated = conn.execute(
                    """
                    UPDATE vlm_processing_jobs
                    SET status = 'cancelling',
                        updated_at = %s
                    WHERE job_id = %s
                    RETURNING *
                    """,
                    (now, job_id),
                ).fetchone()
            else:
                updated = conn.execute(
                    "SELECT * FROM vlm_processing_jobs WHERE job_id = %s",
                    (job_id,),
                ).fetchone()
        return self._format_job_row(updated)

    def existing_image_ids(self, image_ids: list[str]) -> set[str]:
        if not self.database_url or not image_ids:
            return set()
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT image_id
                FROM vlm_image_analysis
                WHERE image_id = ANY(%s)
                    AND error IS NULL
                """,
                (image_ids,),
            ).fetchall()
        return {row["image_id"] for row in rows}

    def update_image_metadata(
        self,
        grid_id: str,
        image: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        self.ensure_schema()
        image_id = _image_id(image)
        if not image_id:
            return None
        image_properties = image.get("properties") or {}
        geometry = preferred_mapillary_geometry(image.get("geometry"), image_properties)
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE vlm_image_analysis
                SET grid_id = %s,
                    geometry = COALESCE(%s::jsonb, geometry),
                    image_properties = COALESCE(image_properties, '{}'::jsonb) || %s::jsonb,
                    updated_at = %s
                WHERE image_id = %s
                RETURNING image_id, grid_id, model, prompt_version, geometry, image_properties, fields, error, updated_at
                """,
                (
                    grid_id,
                    json.dumps(geometry, ensure_ascii=False) if geometry else None,
                    json.dumps(image_properties, ensure_ascii=False),
                    now,
                    image_id,
                ),
            ).fetchone()
        return self._format_row(row) if row else None

    def upsert(self, grid_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self.ensure_schema()
        image_id = str(result["image_id"])
        fields = {field: result.get(field) for field in VLM_FIELDS}
        fields["confidence"] = result.get("confidence")
        fields["reason"] = result.get("reason")
        image_properties = result.get("image_properties") or {}
        geometry = preferred_mapillary_geometry(result.get("geometry"), image_properties)
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO vlm_image_analysis
                    (image_id, grid_id, model, prompt_version, geometry, image_properties, fields, raw_response, error, updated_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
                ON CONFLICT (image_id) DO UPDATE SET
                    grid_id = EXCLUDED.grid_id,
                    model = EXCLUDED.model,
                    prompt_version = EXCLUDED.prompt_version,
                    geometry = EXCLUDED.geometry,
                    image_properties = EXCLUDED.image_properties,
                    fields = EXCLUDED.fields,
                    raw_response = EXCLUDED.raw_response,
                    error = EXCLUDED.error,
                    updated_at = EXCLUDED.updated_at
                RETURNING image_id, grid_id, model, prompt_version, geometry, image_properties, fields, error, updated_at
                """,
                (
                    image_id,
                    grid_id,
                    result.get("model", ""),
                    result.get("prompt_version", ""),
                    json.dumps(geometry, ensure_ascii=False),
                    json.dumps(image_properties, ensure_ascii=False),
                    json.dumps(fields, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    result.get("error"),
                    now,
                ),
            ).fetchone()
        return self._format_row(row)

    def results_for_grid(self, grid_id: str) -> dict[str, Any]:
        if not self.database_url:
            return {"grid_id": grid_id, "available": False, "results": {}}
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT image_id, grid_id, model, prompt_version, geometry, image_properties, fields, error, updated_at
                FROM vlm_image_analysis
                WHERE grid_id = %s
                ORDER BY updated_at DESC
                """,
                (grid_id,),
            ).fetchall()
        results = {row["image_id"]: self._format_row(row) for row in rows}
        return {
            "grid_id": grid_id,
            "available": True,
            "count": len(results),
            "results": results,
        }

    def all_results(self, limit: int = 5000) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "results": {}}
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT image_id, grid_id, model, prompt_version, geometry, image_properties, fields, error, updated_at
                FROM vlm_image_analysis
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        results = {row["image_id"]: self._format_row(row) for row in rows}
        return {
            "available": True,
            "count": len(results),
            "limit": limit,
            "results": results,
        }

    def delete_result(self, image_id: str) -> bool:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self.ensure_schema()
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM vlm_image_analysis WHERE image_id = %s",
                (image_id,),
            )
        return bool(result.rowcount)

    def delete_results_for_grid(self, grid_id: str) -> int:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self.ensure_schema()
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM vlm_image_analysis WHERE grid_id = %s",
                (grid_id,),
            )
        return int(result.rowcount or 0)

    def _connect(self) -> psycopg.Connection:
        assert self.database_url is not None
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=3)

    def _format_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "image_id": row["image_id"],
            "grid_id": row["grid_id"],
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "geometry": row["geometry"],
            "image_properties": row["image_properties"],
            "fields": row["fields"],
            "error": row["error"],
            "updated_at": row["updated_at"].isoformat(),
        }

    def _format_job_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "grid_id": row["grid_id"],
            "model": row["model"],
            "force": row["force"],
            "status": row["status"],
            "total": row["total"],
            "processed": row["processed"],
            "analyzed": row["analyzed"],
            "skipped": row["skipped"],
            "failed": row["failed"],
            "analysis_seconds_sum": float(row.get("analysis_seconds_sum") or 0),
            "current_image_id": row["current_image_id"],
            "last_stored_image_id": row["last_stored_image_id"],
            "error": row["error"],
            "created_at": _iso(row["created_at"]),
            "started_at": _iso(row["started_at"]),
            "updated_at": _iso(row["updated_at"]),
            "completed_at": _iso(row["completed_at"]),
        }


def _image_id(image: dict[str, Any]) -> str:
    return str(image.get("id") or image.get("properties", {}).get("id") or "")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
