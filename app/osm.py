from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row


REQUIRED_TABLES = ("osm_roads", "osm_buildings", "osm_landuse")


@dataclass(frozen=True)
class OSMStore:
    database_url: str | None

    @property
    def configured(self) -> bool:
        return bool(self.database_url)

    def status(self) -> dict[str, Any]:
        if not self.database_url:
            return {"configured": False, "connected": False, "available": False}

        try:
            with self._connect() as conn:
                available = self._tables_available(conn)
                counts = self._counts(conn) if available else {}
                return {
                    "configured": True,
                    "connected": True,
                    "available": available,
                    "counts": counts,
                }
        except psycopg.Error as exc:
            return {
                "configured": True,
                "connected": False,
                "available": False,
                "error": exc.__class__.__name__,
            }

    def cell_layers(self, grid_id: str, cell_geometry: dict) -> dict[str, Any]:
        if not self.database_url:
            return self._unavailable(grid_id, "DATABASE_URL is not configured")

        cell_geojson = json.dumps(cell_geometry)
        try:
            with self._connect() as conn:
                if not self._tables_available(conn):
                    return self._unavailable(grid_id, "OSM tables have not been imported")
                roads = self._features(
                    conn,
                    grid_id,
                    cell_geojson,
                    table="osm_roads",
                    geom_column="geom_mainz",
                    geom_type=2,
                    columns=(
                        "osm_id",
                        "tags",
                        "name",
                        "highway",
                        "maxspeed",
                        "oneway",
                    ),
                    measure_sql="ST_Length(ST_Intersection(geom_mainz, cell.geom)::geography)",
                )
                buildings = self._features(
                    conn,
                    grid_id,
                    cell_geojson,
                    table="osm_buildings",
                    geom_column="geom_mainz",
                    geom_type=3,
                    columns=("osm_id", "tags", "name", "building"),
                    measure_sql="ST_Area(ST_Intersection(geom_mainz, cell.geom)::geography)",
                )
                landuse = self._features(
                    conn,
                    grid_id,
                    cell_geojson,
                    table="osm_landuse",
                    geom_column="geom_mainz",
                    geom_type=3,
                    columns=("osm_id", "tags", "name", "kind", "class_name"),
                    measure_sql="ST_Area(ST_Intersection(geom_mainz, cell.geom)::geography)",
                )
        except psycopg.Error as exc:
            return self._unavailable(grid_id, exc.__class__.__name__)

        return {
            "grid_id": grid_id,
            "available": True,
            "layers": {
                "roads": roads,
                "buildings": buildings,
                "landuse": landuse,
            },
            "meta": {
                "counts": {
                    "roads": len(roads["features"]),
                    "buildings": len(buildings["features"]),
                    "landuse": len(landuse["features"]),
                },
                "geometry": "clipped_to_cell",
                "road_full_geometry_endpoint": "/api/osm/roads/{osm_id}",
            },
        }

    def road(self, osm_id: int) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        sql = """
            SELECT
                osm_id,
                tags,
                name,
                highway,
                maxspeed,
                oneway,
                ST_AsGeoJSON(geom, 6) AS geometry,
                ST_AsGeoJSON(geom_mainz, 6) AS mainz_geometry
            FROM osm_roads
            WHERE osm_id = %s
        """
        try:
            with self._connect() as conn:
                if not self._tables_available(conn):
                    return None
                row = conn.execute(sql, (osm_id,)).fetchone()
        except psycopg.Error:
            return None
        if row is None:
            return None
        properties = {
            key: row[key]
            for key in ("osm_id", "tags", "name", "highway", "maxspeed", "oneway")
        }
        return {
            "type": "Feature",
            "id": f"road/{row['osm_id']}",
            "geometry": json.loads(row["geometry"]),
            "properties": {
                **properties,
                "mainz_geometry": json.loads(row["mainz_geometry"]),
            },
        }

    def _connect(self) -> psycopg.Connection:
        assert self.database_url is not None
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=3)

    def _tables_available(self, conn: psycopg.Connection) -> bool:
        rows = conn.execute(
            """
            SELECT to_regclass('public.' || table_name) IS NOT NULL AS present
            FROM unnest(%s::text[]) AS table_name
            """,
            (list(REQUIRED_TABLES),),
        ).fetchall()
        return bool(rows) and all(row["present"] for row in rows)

    def _counts(self, conn: psycopg.Connection) -> dict[str, int]:
        return {
            "roads": conn.execute("SELECT count(*) AS count FROM osm_roads").fetchone()[
                "count"
            ],
            "buildings": conn.execute(
                "SELECT count(*) AS count FROM osm_buildings"
            ).fetchone()["count"],
            "landuse": conn.execute(
                "SELECT count(*) AS count FROM osm_landuse"
            ).fetchone()["count"],
        }

    def _features(
        self,
        conn: psycopg.Connection,
        grid_id: str,
        cell_geojson: str,
        *,
        table: str,
        geom_column: str,
        geom_type: int,
        columns: tuple[str, ...],
        measure_sql: str,
    ) -> dict[str, Any]:
        select_columns = ", ".join(columns)
        sql = f"""
            WITH cell AS (
                SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) AS geom
            ),
            clipped AS (
                SELECT
                    {select_columns},
                    ST_Multi(
                        ST_CollectionExtract(
                            ST_Intersection({geom_column}, cell.geom),
                            {geom_type}
                        )
                    ) AS geometry,
                    {measure_sql} AS measure
                FROM {table}, cell
                WHERE ST_Intersects({geom_column}, cell.geom)
            )
            SELECT
                {select_columns},
                measure,
                ST_AsGeoJSON(geometry, 6) AS geometry
            FROM clipped
            WHERE NOT ST_IsEmpty(geometry)
            ORDER BY osm_id
        """
        rows = conn.execute(sql, (cell_geojson,)).fetchall()
        features = []
        measure_name = "length_m" if geom_type == 2 else "area_m2"
        for row in rows:
            geometry = row.pop("geometry")
            measure = row.pop("measure")
            features.append(
                {
                    "type": "Feature",
                    "id": f"{table}/{row['osm_id']}",
                    "geometry": json.loads(geometry),
                    "properties": {
                        **row,
                        "grid_id": grid_id,
                        measure_name: round(float(measure or 0), 2),
                    },
                }
            )
        return {"type": "FeatureCollection", "features": features}

    def _unavailable(self, grid_id: str, reason: str) -> dict[str, Any]:
        return {
            "grid_id": grid_id,
            "available": False,
            "reason": reason,
            "layers": {
                "roads": {"type": "FeatureCollection", "features": []},
                "buildings": {"type": "FeatureCollection", "features": []},
                "landuse": {"type": "FeatureCollection", "features": []},
            },
            "meta": {"counts": {"roads": 0, "buildings": 0, "landuse": 0}},
        }
