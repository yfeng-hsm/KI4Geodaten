from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True)
class MapillaryPositionStore:
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
                CREATE TABLE IF NOT EXISTS mapillary_image_positions (
                    image_id text PRIMARY KEY,
                    grid_id text NOT NULL,
                    sequence_id text,
                    gps_geometry jsonb,
                    mapillary_geometry jsonb,
                    mapmatched_geometry jsonb NOT NULL,
                    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS mapillary_image_positions_grid_idx
                ON mapillary_image_positions (grid_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS mapillary_image_positions_sequence_idx
                ON mapillary_image_positions (sequence_id)
                """
            )

    def upsert_many(self, grid_id: str, features: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        self.ensure_schema()
        now = datetime.now(timezone.utc)
        saved: list[dict[str, Any]] = []
        rows_by_image_id: dict[str, dict[str, Any]] = {}
        for feature in features:
            row = _position_row(grid_id, feature)
            if row is not None:
                rows_by_image_id[row["image_id"]] = row
        with self._connect() as conn:
            for row in rows_by_image_id.values():
                stored = conn.execute(
                    """
                    INSERT INTO mapillary_image_positions
                        (image_id, grid_id, sequence_id, gps_geometry, mapillary_geometry, mapmatched_geometry, metadata, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                    ON CONFLICT (image_id) DO UPDATE SET
                        grid_id = EXCLUDED.grid_id,
                        sequence_id = EXCLUDED.sequence_id,
                        gps_geometry = EXCLUDED.gps_geometry,
                        mapillary_geometry = EXCLUDED.mapillary_geometry,
                        mapmatched_geometry = EXCLUDED.mapmatched_geometry,
                        metadata = EXCLUDED.metadata,
                        updated_at = EXCLUDED.updated_at
                    RETURNING *
                    """,
                    (
                        row["image_id"],
                        row["grid_id"],
                        row["sequence_id"],
                        json.dumps(row["gps_geometry"], ensure_ascii=False) if row["gps_geometry"] else None,
                        json.dumps(row["mapillary_geometry"], ensure_ascii=False) if row["mapillary_geometry"] else None,
                        json.dumps(row["mapmatched_geometry"], ensure_ascii=False),
                        json.dumps(row["metadata"], ensure_ascii=False),
                        now,
                    ),
                ).fetchone()
                saved.append(self._format_row(stored))
        return {"available": True, "grid_id": grid_id, "count": len(saved), "features": saved}

    def features_for_grid(self, grid_id: str) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "features": []}
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM mapillary_image_positions
                WHERE grid_id = %s
                ORDER BY updated_at DESC
                """,
                (grid_id,),
            ).fetchall()
        return {"available": True, "grid_id": grid_id, "count": len(rows), "features": [self._format_row(row) for row in rows]}

    def all_features(self, limit: int = 50000) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "features": []}
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM mapillary_image_positions
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return {"available": True, "count": len(rows), "limit": limit, "features": [self._format_row(row) for row in rows]}

    def _connect(self) -> psycopg.Connection:
        assert self.database_url is not None
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=3)

    def _format_row(self, row: dict[str, Any]) -> dict[str, Any]:
        image_id = row["image_id"]
        properties = {
            "image_id": image_id,
            "id": image_id,
            "grid_id": row["grid_id"],
            "sequence_id": row["sequence_id"],
            "gps_geometry": row["gps_geometry"],
            "original_geometry": row["gps_geometry"],
            "mapillary_geometry": row["mapillary_geometry"],
            "computed_geometry": row["mapillary_geometry"],
            "mapmatched_geometry": row["mapmatched_geometry"],
            "mapmatched_confirmed": True,
            "updated_at": row["updated_at"].isoformat(),
            **(row["metadata"] or {}),
        }
        return {
            "type": "Feature",
            "id": image_id,
            "geometry": row["mapmatched_geometry"],
            "properties": properties,
        }


def _position_row(grid_id: str, feature: dict[str, Any]) -> dict[str, Any] | None:
    properties = feature.get("properties") or {}
    image_id = str(feature.get("id") or properties.get("image_id") or properties.get("id") or "")
    mapmatched_geometry = properties.get("mapmatched_geometry") or feature.get("geometry")
    if not image_id or not _valid_point(mapmatched_geometry):
        return None
    return {
        "image_id": image_id,
        "grid_id": grid_id,
        "sequence_id": _optional_text(properties.get("sequence_id")),
        "gps_geometry": properties.get("gps_geometry") or properties.get("original_geometry"),
        "mapillary_geometry": properties.get("mapillary_geometry") or properties.get("computed_geometry"),
        "mapmatched_geometry": mapmatched_geometry,
        "metadata": {
            key: properties.get(key)
            for key in (
                "mapmatched_source",
                "mapmatched_segment_index",
                "mapmatched_idx",
                "mapmatched_profile",
                "segment_index",
                "idx",
                "profile",
            )
            if properties.get(key) is not None
        },
    }


def _valid_point(geometry: Any) -> bool:
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return False
    coordinates = geometry.get("coordinates")
    return isinstance(coordinates, list) and len(coordinates) >= 2


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
