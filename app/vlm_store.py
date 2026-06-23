from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.ollama import VLM_FIELDS


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
                CREATE INDEX IF NOT EXISTS vlm_image_analysis_grid_idx
                ON vlm_image_analysis (grid_id)
                """
            )

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
                """,
                (image_ids,),
            ).fetchall()
        return {row["image_id"] for row in rows}

    def upsert(self, grid_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self.ensure_schema()
        image_id = str(result["image_id"])
        fields = {field: result.get(field) for field in VLM_FIELDS}
        fields["confidence"] = result.get("confidence")
        fields["reason"] = result.get("reason")
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO vlm_image_analysis
                    (image_id, grid_id, model, prompt_version, geometry, fields, raw_response, error, updated_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
                ON CONFLICT (image_id) DO UPDATE SET
                    grid_id = EXCLUDED.grid_id,
                    model = EXCLUDED.model,
                    prompt_version = EXCLUDED.prompt_version,
                    geometry = EXCLUDED.geometry,
                    fields = EXCLUDED.fields,
                    raw_response = EXCLUDED.raw_response,
                    error = EXCLUDED.error,
                    updated_at = EXCLUDED.updated_at
                RETURNING image_id, grid_id, model, prompt_version, geometry, fields, error, updated_at
                """,
                (
                    image_id,
                    grid_id,
                    result.get("model", ""),
                    result.get("prompt_version", ""),
                    json.dumps(result.get("geometry"), ensure_ascii=False),
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
                SELECT image_id, grid_id, model, prompt_version, geometry, fields, error, updated_at
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
            "fields": row["fields"],
            "error": row["error"],
            "updated_at": row["updated_at"].isoformat(),
        }
