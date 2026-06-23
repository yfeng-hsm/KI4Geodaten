from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row


REQUIRED_TABLES = ("osm_roads", "osm_buildings", "osm_landuse")


def _round_optional(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _increment_count(counter: dict[str, int], value: Any) -> None:
    key = "null" if value is None else str(value)
    counter[key] = counter.get(key, 0) + 1


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

    def road_vlm_matches(
        self,
        osm_id: int,
        *,
        max_distance_m: float = 35,
        close_override_m: float = 5,
        view_fov_deg: float = 110,
        on_road_visible_m: float = 1,
        no_heading_visible_m: float = 5,
        road_axis_tolerance_deg: float = 35,
        limit: int = 200,
    ) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        road_category_sql = self._road_category_sql("r")
        nearest_same_category_sql = self._road_category_sql("same_road")
        nearest_compatible_category_sql = self._road_category_sql("compatible_road")
        sql = f"""
            WITH selected AS (
                SELECT
                    r.osm_id,
                    r.tags,
                    r.name,
                    r.highway,
                    r.maxspeed,
                    r.oneway,
                    r.geom_mainz,
                    {road_category_sql} AS road_category
                FROM osm_roads AS r
                WHERE r.osm_id = %s
            ),
            candidates AS (
                SELECT
                    v.image_id,
                    v.grid_id,
                    v.model,
                    v.prompt_version,
                    v.geometry,
                    v.image_properties,
                    v.fields,
                    v.error,
                    v.updated_at,
                    ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326) AS point_geom,
                    COALESCE(
                        NULLIF(v.image_properties->>'computed_compass_angle', '')::double precision,
                        NULLIF(v.image_properties->>'compass_angle', '')::double precision
                    ) AS camera_heading_deg,
                    CASE v.fields->>'capture_position'
                        WHEN 'vehicle_road' THEN 'vehicle'
                        WHEN 'pedestrian_road' THEN 'pedestrian'
                        WHEN 'bicycle_road' THEN 'bicycle'
                        ELSE NULL
                    END AS road_category
                FROM vlm_image_analysis AS v
                WHERE v.geometry IS NOT NULL
                    AND v.fields->>'capture_position' IN (
                        'vehicle_road',
                        'pedestrian_road',
                        'bicycle_road'
                    )
            ),
            nearest AS (
                SELECT
                    c.*,
                    same_road.osm_id AS nearest_same_osm_id,
                    {nearest_same_category_sql} AS nearest_same_road_category,
                    same_road.geom_mainz AS nearest_same_geom,
                    ST_Distance(
                        c.point_geom::geography,
                        same_road.geom_mainz::geography
                    ) AS nearest_same_distance_m,
                    ST_ClosestPoint(same_road.geom_mainz, c.point_geom) AS nearest_same_closest_point,
                    compatible_road.osm_id AS nearest_compatible_osm_id,
                    {nearest_compatible_category_sql} AS nearest_compatible_road_category,
                    compatible_road.geom_mainz AS nearest_compatible_geom,
                    ST_Distance(
                        c.point_geom::geography,
                        compatible_road.geom_mainz::geography
                    ) AS nearest_compatible_distance_m,
                    ST_ClosestPoint(compatible_road.geom_mainz, c.point_geom) AS nearest_compatible_closest_point
                FROM candidates AS c
                LEFT JOIN LATERAL (
                    SELECT same_road.*
                    FROM osm_roads AS same_road
                    WHERE {nearest_same_category_sql} = c.road_category
                    ORDER BY same_road.geom_mainz <-> c.point_geom
                    LIMIT 1
                ) AS same_road ON true
                CROSS JOIN LATERAL (
                    SELECT compatible_road.*
                    FROM osm_roads AS compatible_road
                    WHERE c.road_category <> 'vehicle'
                        OR {nearest_compatible_category_sql} = 'vehicle'
                    ORDER BY compatible_road.geom_mainz <-> c.point_geom
                    LIMIT 1
                ) AS compatible_road
            ),
            assigned AS (
                SELECT
                    n.*,
                    s.osm_id AS selected_osm_id,
                    s.name AS selected_name,
                    s.highway AS selected_highway,
                    s.road_category AS selected_road_category,
                    s.geom_mainz AS selected_geom,
                    CASE
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_osm_id
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_osm_id
                        ELSE NULL
                    END AS assigned_osm_id,
                    CASE
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_road_category
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_road_category
                        ELSE NULL
                    END AS assigned_road_category,
                    CASE
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_distance_m
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_distance_m
                        ELSE NULL
                    END AS distance_m,
                    CASE
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_closest_point
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_closest_point
                        ELSE NULL
                    END AS closest_point,
                    CASE
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_geom
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_geom
                        ELSE NULL
                    END AS assigned_geom,
                    CASE
                        WHEN n.nearest_compatible_distance_m <= %s THEN 'close_compatible_type_nearest'
                        WHEN n.nearest_same_distance_m <= %s THEN 'same_type_nearest'
                        ELSE NULL
                    END AS match_method
                FROM nearest AS n
                CROSS JOIN selected AS s
            ),
            visible AS (
                SELECT
                    a.*,
                    road_axis.road_bearing_deg,
                    CASE
                        WHEN a.distance_m <= %s THEN 0.0
                        WHEN a.camera_heading_deg IS NULL THEN NULL
                        ELSE degrees(ST_Azimuth(a.point_geom::geography, a.closest_point::geography))
                    END AS target_bearing_deg,
                    CASE
                        WHEN a.distance_m <= %s THEN 0.0
                        WHEN a.camera_heading_deg IS NULL THEN NULL
                        ELSE LEAST(
                            ABS(
                                MOD(
                                    (degrees(ST_Azimuth(a.point_geom::geography, a.closest_point::geography))
                                        - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                        + 540.0)::numeric,
                                    360.0::numeric
                                )::double precision - 180.0
                            ),
                            180.0
                        )
                    END AS view_delta_deg,
                    CASE
                        WHEN a.distance_m <= %s THEN 0.0
                        WHEN a.camera_heading_deg IS NULL OR road_axis.road_bearing_deg IS NULL THEN NULL
                        ELSE LEAST(
                            LEAST(
                                ABS(
                                    MOD(
                                        (road_axis.road_bearing_deg
                                            - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                            + 540.0)::numeric,
                                        360.0::numeric
                                    )::double precision - 180.0
                                ),
                                180.0
                            ),
                            ABS(
                                180.0 - LEAST(
                                    ABS(
                                        MOD(
                                            (road_axis.road_bearing_deg
                                                - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                                + 540.0)::numeric,
                                            360.0::numeric
                                        )::double precision - 180.0
                                    ),
                                    180.0
                                )
                            )
                        )
                    END AS road_axis_delta_deg,
                    CASE
                        WHEN a.distance_m <= %s THEN true
                        WHEN a.camera_heading_deg IS NULL THEN a.distance_m <= %s
                        ELSE (
                            LEAST(
                                ABS(
                                    MOD(
                                        (degrees(ST_Azimuth(a.point_geom::geography, a.closest_point::geography))
                                            - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                            + 540.0)::numeric,
                                        360.0::numeric
                                    )::double precision - 180.0
                                ),
                                180.0
                            ) <= %s
                            OR (
                                road_axis.road_bearing_deg IS NOT NULL
                                AND LEAST(
                                    LEAST(
                                        ABS(
                                            MOD(
                                                (road_axis.road_bearing_deg
                                                    - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                                    + 540.0)::numeric,
                                                360.0::numeric
                                            )::double precision - 180.0
                                        ),
                                        180.0
                                    ),
                                    ABS(
                                        180.0 - LEAST(
                                            ABS(
                                                MOD(
                                                    (road_axis.road_bearing_deg
                                                        - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                                        + 540.0)::numeric,
                                                    360.0::numeric
                                                )::double precision - 180.0
                                            ),
                                            180.0
                                        )
                                    )
                                ) <= %s
                            )
                        )
                    END AS within_view_cone,
                    ST_AsGeoJSON(a.point_geom, 6) AS point_geometry,
                    ST_AsGeoJSON(
                        ST_MakeLine(
                            a.point_geom,
                            a.closest_point
                        ),
                        6
                    ) AS link_geometry
                FROM assigned AS a
                LEFT JOIN LATERAL (
                    SELECT
                        degrees(ST_Azimuth(
                            ST_PointN(dumped.geom, segment_index)::geography,
                            ST_PointN(dumped.geom, segment_index + 1)::geography
                        )) AS road_bearing_deg
                    FROM ST_Dump(a.assigned_geom) AS dumped
                    CROSS JOIN LATERAL generate_series(1, GREATEST(ST_NPoints(dumped.geom) - 1, 0)) AS segment(segment_index)
                    WHERE a.assigned_geom IS NOT NULL
                        AND ST_NPoints(dumped.geom) > 1
                    ORDER BY ST_MakeLine(
                        ST_PointN(dumped.geom, segment_index),
                        ST_PointN(dumped.geom, segment_index + 1)
                    ) <-> a.point_geom
                    LIMIT 1
                ) AS road_axis ON true
            )
            SELECT *
            FROM visible
            WHERE match_method IS NOT NULL
                AND assigned_osm_id = selected_osm_id
                AND within_view_cone
            ORDER BY distance_m ASC
            LIMIT %s
        """
        road_sql = f"""
            SELECT
                r.osm_id,
                r.tags,
                r.name,
                r.highway,
                r.maxspeed,
                r.oneway,
                r.tags->>'surface' AS surface,
                {road_category_sql} AS road_category,
                ST_AsGeoJSON(r.geom_mainz, 6) AS geometry
            FROM osm_roads AS r
            WHERE r.osm_id = %s
        """
        try:
            with self._connect() as conn:
                if not self._tables_available(conn):
                    return None
                road_row = conn.execute(road_sql, (osm_id,)).fetchone()
                if road_row is None:
                    return None
                rows = conn.execute(
                    sql,
                    (
                        osm_id,
                        close_override_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        on_road_visible_m,
                        on_road_visible_m,
                        on_road_visible_m,
                        on_road_visible_m,
                        no_heading_visible_m,
                        view_fov_deg / 2,
                        road_axis_tolerance_deg,
                        limit,
                    ),
                ).fetchall()
        except psycopg.Error:
            return None

        road_geometry = road_row.pop("geometry")
        road_category = road_row["road_category"]
        link_features = []
        point_features = []
        stats = {
            "capture_position": {},
            "surface_material": {},
            "traffic_signal": {},
            "bench": {},
            "waste_basket": {},
            "independent_bicycle_road": {},
            "independent_pedestrian_road": {},
            "match_method": {},
            "image_road_category": {},
        }
        for row in rows:
            link_geometry = row["link_geometry"]
            point_geometry = row["point_geometry"]
            fields = row["fields"] or {}
            image_properties = row["image_properties"] or {}
            _increment_count(stats["capture_position"], fields.get("capture_position"))
            _increment_count(stats["surface_material"], fields.get("surface_material"))
            _increment_count(stats["traffic_signal"], fields.get("traffic_signal"))
            _increment_count(stats["bench"], fields.get("bench"))
            _increment_count(stats["waste_basket"], fields.get("waste_basket"))
            _increment_count(stats["independent_bicycle_road"], fields.get("independent_bicycle_road"))
            _increment_count(stats["independent_pedestrian_road"], fields.get("independent_pedestrian_road"))
            _increment_count(stats["match_method"], row["match_method"])
            _increment_count(stats["image_road_category"], row["road_category"])
            properties = {
                "image_id": row["image_id"],
                "grid_id": row["grid_id"],
                "road_osm_id": row["selected_osm_id"],
                "road_category": row["selected_road_category"],
                "image_road_category": row["road_category"],
                "match_method": row["match_method"],
                "capture_position": fields.get("capture_position"),
                "surface_material": fields.get("surface_material"),
                "distance_m": round(float(row["distance_m"] or 0), 2),
                "camera_heading_deg": _round_optional(row["camera_heading_deg"]),
                "target_bearing_deg": _round_optional(row["target_bearing_deg"]),
                "view_delta_deg": _round_optional(row["view_delta_deg"]),
                "road_bearing_deg": _round_optional(row["road_bearing_deg"]),
                "road_axis_delta_deg": _round_optional(row["road_axis_delta_deg"]),
                "updated_at": row["updated_at"].isoformat(),
                "thumb_256_url": image_properties.get("thumb_256_url"),
                "thumb_1024_url": image_properties.get("thumb_1024_url"),
            }
            link_features.append(
                {
                    "type": "Feature",
                    "id": f"road-vlm-link/{osm_id}/{row['image_id']}",
                    "geometry": json.loads(link_geometry),
                    "properties": properties,
                }
            )
            point_features.append(
                {
                    "type": "Feature",
                    "id": f"road-vlm-point/{osm_id}/{row['image_id']}",
                    "geometry": json.loads(point_geometry),
                    "properties": properties,
                }
            )

        return {
            "road": {
                "type": "Feature",
                "id": f"road/{road_row['osm_id']}",
                "geometry": json.loads(road_geometry),
                "properties": {**dict(road_row), "match_stats": stats},
            },
            "matches": {"type": "FeatureCollection", "features": link_features},
            "points": {"type": "FeatureCollection", "features": point_features},
            "stats": stats,
            "meta": {
                "road_osm_id": osm_id,
                "road_category": road_category,
                "max_distance_m": max_distance_m,
                "close_override_m": close_override_m,
                "view_fov_deg": view_fov_deg,
                "on_road_visible_m": on_road_visible_m,
                "no_heading_visible_m": no_heading_visible_m,
                "road_axis_tolerance_deg": road_axis_tolerance_deg,
                "count": len(link_features),
                "method": "unique_nearest_compatible_road_with_camera_view_or_road_axis_alignment",
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

    def _road_category_sql(self, alias: str) -> str:
        return f"""
            CASE
                WHEN {alias}.highway = 'cycleway'
                    OR {alias}.tags->>'bicycle' = 'designated'
                    THEN 'bicycle'
                WHEN {alias}.highway IN (
                    'footway',
                    'path',
                    'steps',
                    'pedestrian',
                    'platform',
                    'corridor',
                    'elevator'
                )
                    THEN 'pedestrian'
                ELSE 'vehicle'
            END
        """

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
