from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

import psycopg
from psycopg.rows import dict_row


REQUIRED_TABLES = ("osm_roads", "osm_buildings", "osm_landuse")

OSM_SURFACE_GROUPS = {
    "asphalt": "asphalt",
    "concrete": "concrete",
    "concrete:lanes": "concrete",
    "concrete:plates": "concrete",
    "cement": "concrete",
    "paving_stones": "paving_stones",
    "paving_stones:30": "paving_stones",
    "sett": "sett",
    "cobblestone": "sett",
    "unhewn_cobblestone": "sett",
    "bricks": "paving_stones",
    "unpaved": "unpaved",
    "compacted": "unpaved",
    "fine_gravel": "unpaved",
    "gravel": "unpaved",
    "pebblestone": "unpaved",
    "ground": "unpaved",
    "dirt": "unpaved",
    "earth": "unpaved",
    "grass": "unpaved",
    "grass_paver": "unpaved",
    "sand": "unpaved",
    "mud": "unpaved",
    "woodchips": "unpaved",
}

VLM_SURFACE_GROUPS = {
    "asphalt": "asphalt",
    "concrete": "concrete",
    "paving_stones": "paving_stones",
    "sett": "sett",
    "unpaved": "unpaved",
}


def _round_optional(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _increment_count(counter: dict[str, int], value: Any) -> None:
    key = "null" if value is None else str(value)
    counter[key] = counter.get(key, 0) + 1


def normalize_osm_surface(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    return OSM_SURFACE_GROUPS.get(key)


def normalize_vlm_surface(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    return VLM_SURFACE_GROUPS.get(key)


def grouped_surface_counts(counts: dict[str, int]) -> dict[str, int]:
    grouped: dict[str, int] = {}
    for value, count in counts.items():
        group = normalize_vlm_surface(value)
        if group is None:
            continue
        grouped[group] = grouped.get(group, 0) + int(count)
    return grouped


def majority_surface(counts: dict[str, int]) -> str | None:
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def relative_side(camera_heading_deg: Any, target_bearing_deg: Any) -> str | None:
    if camera_heading_deg is None or target_bearing_deg is None:
        return None
    signed_delta = (
        (float(target_bearing_deg) - (float(camera_heading_deg) % 360.0) + 540.0) % 360.0
    ) - 180.0
    if abs(signed_delta) < 15:
        return "front"
    if abs(abs(signed_delta) - 180.0) < 15:
        return "back"
    return "right" if signed_delta > 0 else "left"


def matched_road_surface_material(
    fields: dict[str, Any],
    image_road_category: str | None,
    selected_road_category: str | None,
    side: str | None,
) -> tuple[str | None, str]:
    if image_road_category and image_road_category == selected_road_category:
        return fields.get("surface_material"), "capture_position"
    candidate_sides = [side] if side in {"left", "right"} else ["left", "right"]
    if fields.get("capture_position") == "vehicle_road" and selected_road_category == "pedestrian":
        for candidate_side in candidate_sides:
            sidewalk_key = f"{candidate_side}_sidewalk"
            surface_key = f"{candidate_side}_sidewalk_surface_material"
            if fields.get(sidewalk_key) == "yes":
                return fields.get(surface_key), f"{candidate_side}_sidewalk_from_vehicle"
        sidewalk_sides = [
            candidate_side
            for candidate_side in ("left", "right")
            if fields.get(f"{candidate_side}_sidewalk") == "yes"
        ]
        if len(sidewalk_sides) == 1:
            candidate_side = sidewalk_sides[0]
            return (
                fields.get(f"{candidate_side}_sidewalk_surface_material"),
                f"{candidate_side}_sidewalk_from_vehicle_nearest",
            )
    if fields.get("capture_position") != "pedestrian_road":
        return None, "none"
    if selected_road_category not in {"vehicle", "bicycle"}:
        return None, "none"

    selected_type = f"{selected_road_category}_road"
    for candidate_side in candidate_sides:
        type_key = f"{candidate_side}_adjacent_road_type"
        surface_key = f"{candidate_side}_adjacent_road_surface_material"
        if fields.get(type_key) == selected_type:
            return fields.get(surface_key), f"{candidate_side}_adjacent_road"
    return None, "none"


def virtual_observation_side(source: str | None) -> str | None:
    if not source:
        return None
    if source.startswith("left_"):
        return "left"
    if source.startswith("right_"):
        return "right"
    return None


def offset_point_geometry(
    point_geometry: str | dict[str, Any],
    heading_deg: Any,
    side: str | None,
    meters: float = 4.5,
) -> dict[str, Any] | None:
    if side not in {"left", "right"} or heading_deg is None:
        return None
    geometry = json.loads(point_geometry) if isinstance(point_geometry, str) else point_geometry
    if geometry.get("type") != "Point":
        return None
    lon, lat = geometry["coordinates"][:2]
    bearing_deg = (float(heading_deg) + (-90.0 if side == "left" else 90.0)) % 360.0
    bearing = math.radians(bearing_deg)
    radius = 6378137.0
    lat_rad = math.radians(float(lat))
    delta_lat = (meters * math.cos(bearing)) / radius
    cos_lat = max(math.cos(lat_rad), 1e-9)
    delta_lon = (meters * math.sin(bearing)) / (radius * cos_lat)
    return {
        "type": "Point",
        "coordinates": [
            float(lon) + math.degrees(delta_lon),
            float(lat) + math.degrees(delta_lat),
        ],
    }


def replace_link_start(link_geometry: str, start_geometry: dict[str, Any]) -> dict[str, Any]:
    link = json.loads(link_geometry)
    if link.get("type") != "LineString" or len(link.get("coordinates", [])) < 2:
        return link
    link["coordinates"][0] = start_geometry["coordinates"]
    return link


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

    def cell_road_surface_validation(
        self,
        grid_id: str,
        cell_geometry: dict | None,
    ) -> dict[str, Any]:
        if not self.database_url:
            return self._unavailable_surface_validation(grid_id, "DATABASE_URL is not configured")
        cell_geojson = json.dumps(cell_geometry) if cell_geometry is not None else None
        road_category_sql = self._road_category_sql("r")
        selected_road_category_sql = self._road_category_sql("sr")
        nearest_same_category_sql = self._road_category_sql("same_road")
        nearest_compatible_category_sql = self._road_category_sql("compatible_road")
        sql = f"""
            WITH cell AS (
                SELECT
                    CASE
                        WHEN %s::text IS NULL THEN NULL
                        ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)
                    END AS geom
            ),
            selected_roads AS (
                SELECT
                    r.osm_id,
                    r.tags,
                    r.name,
                    r.highway,
                    r.maxspeed,
                    r.oneway,
                    r.tags->>'surface' AS surface,
                    {road_category_sql} AS road_category,
                    CASE
                        WHEN cell.geom IS NULL THEN ST_Length(r.geom_mainz::geography)
                        ELSE ST_Length(ST_Intersection(r.geom_mainz, cell.geom)::geography)
                    END AS length_m,
                    CASE
                        WHEN cell.geom IS NULL THEN r.geom_mainz
                        ELSE ST_Multi(
                            ST_CollectionExtract(
                                ST_Intersection(r.geom_mainz, cell.geom),
                                2
                            )
                        )
                    END AS geometry
                FROM osm_roads AS r, cell
                WHERE r.geom_mainz IS NOT NULL
                    AND (cell.geom IS NULL OR ST_Intersects(r.geom_mainz, cell.geom))
            ),
            candidates AS (
                SELECT
                    v.image_id,
                    v.fields,
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
                    AND COALESCE(v.fields->>'unusable_reason', 'none') = 'none'
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
                    ST_Distance(c.point_geom::geography, same_road.geom_mainz::geography) AS nearest_same_distance_m,
                    ST_ClosestPoint(same_road.geom_mainz, c.point_geom) AS nearest_same_closest_point,
                    compatible_road.osm_id AS nearest_compatible_osm_id,
                    {nearest_compatible_category_sql} AS nearest_compatible_road_category,
                    compatible_road.geom_mainz AS nearest_compatible_geom,
                    ST_Distance(c.point_geom::geography, compatible_road.geom_mainz::geography) AS nearest_compatible_distance_m,
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
                    WHERE CASE
                        WHEN c.road_category = 'vehicle' THEN {nearest_compatible_category_sql} = 'vehicle'
                        WHEN c.road_category IN ('pedestrian', 'bicycle') THEN {nearest_compatible_category_sql} IN ('vehicle', 'bicycle')
                        ELSE false
                    END
                    ORDER BY compatible_road.geom_mainz <-> c.point_geom
                    LIMIT 1
                ) AS compatible_road
            ),
            assigned AS (
                SELECT
                    n.*,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= 35 THEN n.nearest_same_osm_id
                        WHEN n.nearest_compatible_distance_m <= 5 THEN n.nearest_compatible_osm_id
                        WHEN n.nearest_same_distance_m <= 35 THEN n.nearest_same_osm_id
                        ELSE NULL
                    END AS assigned_osm_id,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= 35 THEN n.nearest_same_road_category
                        WHEN n.nearest_compatible_distance_m <= 5 THEN n.nearest_compatible_road_category
                        WHEN n.nearest_same_distance_m <= 35 THEN n.nearest_same_road_category
                        ELSE NULL
                    END AS assigned_road_category,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= 35 THEN n.nearest_same_distance_m
                        WHEN n.nearest_compatible_distance_m <= 5 THEN n.nearest_compatible_distance_m
                        WHEN n.nearest_same_distance_m <= 35 THEN n.nearest_same_distance_m
                        ELSE NULL
                    END AS distance_m,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= 35 THEN n.nearest_same_closest_point
                        WHEN n.nearest_compatible_distance_m <= 5 THEN n.nearest_compatible_closest_point
                        WHEN n.nearest_same_distance_m <= 35 THEN n.nearest_same_closest_point
                        ELSE NULL
                    END AS closest_point,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= 35 THEN n.nearest_same_geom
                        WHEN n.nearest_compatible_distance_m <= 5 THEN n.nearest_compatible_geom
                        WHEN n.nearest_same_distance_m <= 35 THEN n.nearest_same_geom
                        ELSE NULL
                    END AS assigned_geom
                FROM nearest AS n
            ),
            visible AS (
                SELECT
                    a.*,
                    CASE
                        WHEN a.distance_m <= 1 OR a.camera_heading_deg IS NULL THEN NULL
                        ELSE degrees(ST_Azimuth(a.point_geom::geography, a.closest_point::geography))
                    END AS target_bearing_deg,
                    CASE
                        WHEN a.distance_m <= 1 OR a.camera_heading_deg IS NULL THEN NULL
                        WHEN ABS(
                            (
                                MOD(
                                    (degrees(ST_Azimuth(a.point_geom::geography, a.closest_point::geography))
                                        - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                        + 540.0)::numeric,
                                    360.0::numeric
                                )::double precision - 180.0
                            )
                        ) < 15 THEN 'front'
                        WHEN ABS(ABS(
                            (
                                MOD(
                                    (degrees(ST_Azimuth(a.point_geom::geography, a.closest_point::geography))
                                        - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                        + 540.0)::numeric,
                                    360.0::numeric
                                )::double precision - 180.0
                            )
                        ) - 180.0) < 15 THEN 'back'
                        WHEN (
                            MOD(
                                (degrees(ST_Azimuth(a.point_geom::geography, a.closest_point::geography))
                                    - MOD(a.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                    + 540.0)::numeric,
                                360.0::numeric
                            )::double precision - 180.0
                        ) > 0 THEN 'right'
                        ELSE 'left'
                    END AS target_side,
                    CASE
                        WHEN a.distance_m <= 1 THEN true
                        WHEN a.camera_heading_deg IS NULL THEN a.distance_m <= 5
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
                            ) <= 55
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
                                ) <= 35
                            )
                        )
                    END AS within_view_cone
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
            ),
            surface_observations AS (
                SELECT
                    assigned_osm_id AS osm_id,
                    CASE
                        WHEN road_category = assigned_road_category THEN fields->>'surface_material'
                        WHEN fields->>'capture_position' = 'pedestrian_road'
                            AND assigned_road_category = 'vehicle'
                            AND target_side = 'left'
                            AND fields->>'left_adjacent_road_type' = 'vehicle_road'
                            THEN fields->>'left_adjacent_road_surface_material'
                        WHEN fields->>'capture_position' = 'pedestrian_road'
                            AND assigned_road_category = 'vehicle'
                            AND target_side = 'right'
                            AND fields->>'right_adjacent_road_type' = 'vehicle_road'
                            THEN fields->>'right_adjacent_road_surface_material'
                        WHEN fields->>'capture_position' = 'pedestrian_road'
                            AND assigned_road_category = 'bicycle'
                            AND target_side = 'left'
                            AND fields->>'left_adjacent_road_type' = 'bicycle_road'
                            THEN fields->>'left_adjacent_road_surface_material'
                        WHEN fields->>'capture_position' = 'pedestrian_road'
                            AND assigned_road_category = 'bicycle'
                            AND target_side = 'right'
                            AND fields->>'right_adjacent_road_type' = 'bicycle_road'
                            THEN fields->>'right_adjacent_road_surface_material'
                        WHEN fields->>'capture_position' = 'pedestrian_road'
                            AND assigned_road_category = 'vehicle'
                            AND fields->>'left_adjacent_road_type' = 'vehicle_road'
                            AND COALESCE(fields->>'right_adjacent_road_type', '') <> 'vehicle_road'
                            THEN fields->>'left_adjacent_road_surface_material'
                        WHEN fields->>'capture_position' = 'pedestrian_road'
                            AND assigned_road_category = 'vehicle'
                            AND fields->>'right_adjacent_road_type' = 'vehicle_road'
                            AND COALESCE(fields->>'left_adjacent_road_type', '') <> 'vehicle_road'
                            THEN fields->>'right_adjacent_road_surface_material'
                        WHEN fields->>'capture_position' = 'pedestrian_road'
                            AND assigned_road_category = 'bicycle'
                            AND fields->>'left_adjacent_road_type' = 'bicycle_road'
                            AND COALESCE(fields->>'right_adjacent_road_type', '') <> 'bicycle_road'
                            THEN fields->>'left_adjacent_road_surface_material'
                        WHEN fields->>'capture_position' = 'pedestrian_road'
                            AND assigned_road_category = 'bicycle'
                            AND fields->>'right_adjacent_road_type' = 'bicycle_road'
                            AND COALESCE(fields->>'left_adjacent_road_type', '') <> 'bicycle_road'
                            THEN fields->>'right_adjacent_road_surface_material'
                        ELSE NULL
                    END AS surface_material
                FROM visible
                WHERE assigned_osm_id IS NOT NULL
                    AND within_view_cone
            ),
            surface_count_rows AS (
                SELECT
                    osm_id,
                    surface_material,
                    count(*)::integer AS count
                FROM surface_observations
                WHERE surface_material IN (
                        'asphalt',
                        'concrete',
                        'paving_stones',
                        'sett',
                        'unpaved'
                    )
                GROUP BY osm_id, surface_material
            ),
            surface_counts AS (
                SELECT
                    osm_id,
                    jsonb_object_agg(surface_material, count) AS surface_counts,
                    sum(count)::integer AS usable_surface_count
                FROM surface_count_rows
                GROUP BY osm_id
            )
            SELECT
                sr.osm_id,
                sr.tags,
                sr.name,
                sr.highway,
                sr.maxspeed,
                sr.oneway,
                sr.surface,
                {selected_road_category_sql} AS road_category,
                sr.length_m,
                sc.surface_counts,
                sc.usable_surface_count,
                ST_AsGeoJSON(sr.geometry, 6) AS geometry
            FROM selected_roads AS sr
            LEFT JOIN surface_counts AS sc ON sc.osm_id = sr.osm_id
            WHERE NOT ST_IsEmpty(sr.geometry)
            ORDER BY sr.osm_id
        """
        try:
            with self._connect() as conn:
                if not self._tables_available(conn):
                    return self._unavailable_surface_validation(grid_id, "OSM tables have not been imported")
                rows = conn.execute(sql, (cell_geojson, cell_geojson)).fetchall()
        except psycopg.Error as exc:
            return self._unavailable_surface_validation(grid_id, exc.__class__.__name__)

        features = []
        skipped = {
            "no_osm_surface": 0,
            "unmapped_osm_surface": 0,
            "no_vlm_surface": 0,
            "no_matches": 0,
        }
        for row in rows:
            osm_surface = row["surface"]
            if not osm_surface:
                skipped["no_osm_surface"] += 1
                continue
            osm_surface_group = normalize_osm_surface(osm_surface)
            if osm_surface_group is None:
                skipped["unmapped_osm_surface"] += 1
                continue
            raw_surface_counts = row["surface_counts"] or {}
            if not raw_surface_counts:
                skipped["no_matches"] += 1
                continue
            surface_counts = grouped_surface_counts(raw_surface_counts)
            vlm_surface_group = majority_surface(surface_counts)
            usable_surface_count = sum(surface_counts.values())
            if vlm_surface_group is None:
                skipped["no_vlm_surface"] += 1
                continue
            if usable_surface_count < 3:
                skipped["too_few_vlm_observations"] = skipped.get("too_few_vlm_observations", 0) + 1
                continue

            status = "match" if vlm_surface_group == osm_surface_group else "mismatch"
            row_geometry = row.pop("geometry")
            row_surface_counts = row.pop("surface_counts")
            row_usable_surface_count = row.pop("usable_surface_count")
            features.append(
                {
                    "type": "Feature",
                    "id": f"osm_roads/{row['osm_id']}/surface-validation",
                    "geometry": json.loads(row_geometry),
                    "properties": {
                        **row,
                        "grid_id": grid_id,
                        "length_m": round(float(row["length_m"] or 0), 2),
                        "surface_validation": status,
                        "osm_surface": osm_surface,
                        "osm_surface_group": osm_surface_group,
                        "vlm_surface_group": vlm_surface_group,
                        "vlm_surface_counts": row_surface_counts,
                        "vlm_surface_group_counts": surface_counts,
                        "vlm_match_count": int(row_usable_surface_count or 0),
                        "vlm_usable_surface_count": usable_surface_count,
                    },
                }
            )

        match_count = sum(
            1 for feature in features
            if feature["properties"]["surface_validation"] == "match"
        )
        mismatch_count = sum(
            1 for feature in features
            if feature["properties"]["surface_validation"] == "mismatch"
        )
        return {
            "grid_id": grid_id,
            "available": True,
            "layers": {
                "roads": {
                    "type": "FeatureCollection",
                    "features": features,
                },
            },
            "meta": {
                "count": len(features),
                "match": match_count,
                "mismatch": mismatch_count,
                "skipped": skipped,
                "surface_groups": sorted(set(OSM_SURFACE_GROUPS.values())),
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
        nearest_pedestrian_category_sql = self._road_category_sql("nearest_ped")
        nearest_side_category_sql = self._road_category_sql("nearest_side")
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
                    AND COALESCE(v.fields->>'unusable_reason', 'none') = 'none'
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
                    WHERE CASE
                        WHEN c.road_category = 'vehicle' THEN {nearest_compatible_category_sql} = 'vehicle'
                        WHEN c.road_category IN ('pedestrian', 'bicycle') THEN {nearest_compatible_category_sql} IN ('vehicle', 'bicycle')
                        ELSE false
                    END
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
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= %s THEN n.nearest_same_osm_id
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_osm_id
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_osm_id
                        ELSE NULL
                    END AS assigned_osm_id,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= %s THEN n.nearest_same_road_category
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_road_category
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_road_category
                        ELSE NULL
                    END AS assigned_road_category,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= %s THEN n.nearest_same_distance_m
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_distance_m
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_distance_m
                        ELSE NULL
                    END AS distance_m,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= %s THEN n.nearest_same_closest_point
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_closest_point
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_closest_point
                        ELSE NULL
                    END AS closest_point,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= %s THEN n.nearest_same_geom
                        WHEN n.nearest_compatible_distance_m <= %s THEN n.nearest_compatible_geom
                        WHEN n.nearest_same_distance_m <= %s THEN n.nearest_same_geom
                        ELSE NULL
                    END AS assigned_geom,
                    CASE
                        WHEN n.road_category IN ('pedestrian', 'bicycle')
                            AND n.nearest_same_distance_m <= %s THEN 'same_type_nearest'
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
        adjacent_sidewalk_sql = f"""
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
                    'vehicle' AS road_category
                FROM vlm_image_analysis AS v
                WHERE v.geometry IS NOT NULL
                    AND COALESCE(v.fields->>'unusable_reason', 'none') = 'none'
                    AND v.fields->>'capture_position' = 'vehicle_road'
                    AND (
                        v.fields->>'left_sidewalk' = 'yes'
                        OR v.fields->>'right_sidewalk' = 'yes'
                    )
            ),
            visible AS (
                SELECT
                    c.*,
                    s.osm_id AS selected_osm_id,
                    s.name AS selected_name,
                    s.highway AS selected_highway,
                    s.road_category AS selected_road_category,
                    s.geom_mainz AS selected_geom,
                    s.osm_id AS assigned_osm_id,
                    s.road_category AS assigned_road_category,
                    ST_Distance(c.point_geom::geography, s.geom_mainz::geography) AS distance_m,
                    ST_ClosestPoint(s.geom_mainz, c.point_geom) AS closest_point,
                    s.geom_mainz AS assigned_geom,
                    'adjacent_sidewalk_from_vehicle' AS match_method,
                    road_axis.road_bearing_deg,
                    CASE
                        WHEN ST_Distance(c.point_geom::geography, s.geom_mainz::geography) <= %s THEN 0.0
                        WHEN c.camera_heading_deg IS NULL THEN NULL
                        ELSE degrees(ST_Azimuth(c.point_geom::geography, ST_ClosestPoint(s.geom_mainz, c.point_geom)::geography))
                    END AS target_bearing_deg,
                    CASE
                        WHEN ST_Distance(c.point_geom::geography, s.geom_mainz::geography) <= %s THEN 0.0
                        WHEN c.camera_heading_deg IS NULL THEN NULL
                        ELSE LEAST(
                            ABS(
                                MOD(
                                    (degrees(ST_Azimuth(c.point_geom::geography, ST_ClosestPoint(s.geom_mainz, c.point_geom)::geography))
                                        - MOD(c.camera_heading_deg::numeric, 360.0::numeric)::double precision
                                        + 540.0)::numeric,
                                    360.0::numeric
                                )::double precision - 180.0
                            ),
                            180.0
                        )
                    END AS view_delta_deg,
                    CASE
                        WHEN ST_Distance(c.point_geom::geography, s.geom_mainz::geography) <= %s THEN 0.0
                        WHEN c.camera_heading_deg IS NULL OR road_axis.road_bearing_deg IS NULL THEN NULL
                        ELSE LEAST(
                            LEAST(
                                ABS(
                                    MOD(
                                        (road_axis.road_bearing_deg
                                            - MOD(c.camera_heading_deg::numeric, 360.0::numeric)::double precision
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
                                                - MOD(c.camera_heading_deg::numeric, 360.0::numeric)::double precision
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
                        WHEN ST_Distance(c.point_geom::geography, s.geom_mainz::geography) <= %s THEN true
                        WHEN c.camera_heading_deg IS NULL THEN ST_Distance(c.point_geom::geography, s.geom_mainz::geography) <= %s
                        ELSE (
                            LEAST(
                                ABS(
                                    MOD(
                                        (degrees(ST_Azimuth(c.point_geom::geography, ST_ClosestPoint(s.geom_mainz, c.point_geom)::geography))
                                            - MOD(c.camera_heading_deg::numeric, 360.0::numeric)::double precision
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
                                                    - MOD(c.camera_heading_deg::numeric, 360.0::numeric)::double precision
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
                                                        - MOD(c.camera_heading_deg::numeric, 360.0::numeric)::double precision
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
                    ST_AsGeoJSON(c.point_geom, 6) AS point_geometry,
                    ST_AsGeoJSON(ST_MakeLine(c.point_geom, ST_ClosestPoint(s.geom_mainz, c.point_geom)), 6) AS link_geometry
                FROM candidates AS c
                CROSS JOIN selected AS s
                LEFT JOIN LATERAL (
                    SELECT
                        degrees(ST_Azimuth(
                            ST_PointN(dumped.geom, segment_index)::geography,
                            ST_PointN(dumped.geom, segment_index + 1)::geography
                        )) AS road_bearing_deg
                    FROM ST_Dump(s.geom_mainz) AS dumped
                    CROSS JOIN LATERAL generate_series(1, GREATEST(ST_NPoints(dumped.geom) - 1, 0)) AS segment(segment_index)
                    WHERE ST_NPoints(dumped.geom) > 1
                    ORDER BY ST_MakeLine(
                        ST_PointN(dumped.geom, segment_index),
                        ST_PointN(dumped.geom, segment_index + 1)
                    ) <-> c.point_geom
                    LIMIT 1
                ) AS road_axis ON true
                WHERE s.road_category = 'pedestrian'
                    AND ST_DWithin(c.point_geom::geography, s.geom_mainz::geography, %s)
                    AND s.osm_id = (
                        SELECT nearest_ped.osm_id
                        FROM osm_roads AS nearest_ped
                        WHERE {nearest_pedestrian_category_sql} = 'pedestrian'
                        ORDER BY nearest_ped.geom_mainz <-> c.point_geom
                        LIMIT 1
                    )
            )
            SELECT *
            FROM visible
            ORDER BY distance_m ASC
            LIMIT %s
        """
        side_observation_sql = f"""
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
            source_images AS (
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
                    ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326) AS base_point_geom,
                    COALESCE(
                        NULLIF(v.image_properties->>'computed_compass_angle', '')::double precision,
                        NULLIF(v.image_properties->>'compass_angle', '')::double precision
                    ) AS camera_heading_deg
                FROM vlm_image_analysis AS v
                WHERE v.geometry IS NOT NULL
                    AND COALESCE(v.fields->>'unusable_reason', 'none') = 'none'
            ),
            observations AS (
                SELECT
                    image_id,
                    grid_id,
                    model,
                    prompt_version,
                    geometry,
                    image_properties,
                    fields,
                    error,
                    updated_at,
                    ST_SetSRID(
                        ST_Project(
                            base_point_geom::geography,
                            2.0,
                            radians(MOD((camera_heading_deg - 90.0 + 360.0)::numeric, 360.0)::double precision)
                        )::geometry,
                        4326
                    ) AS point_geom,
                    camera_heading_deg,
                    'pedestrian' AS road_category,
                    fields->>'left_sidewalk_surface_material' AS observation_surface_material,
                    'left_sidewalk_virtual' AS observation_source,
                    'left' AS observation_side
                FROM source_images
                WHERE fields->>'capture_position' = 'vehicle_road'
                    AND fields->>'left_sidewalk' = 'yes'
                    AND camera_heading_deg IS NOT NULL
                UNION ALL
                SELECT
                    image_id,
                    grid_id,
                    model,
                    prompt_version,
                    geometry,
                    image_properties,
                    fields,
                    error,
                    updated_at,
                    ST_SetSRID(
                        ST_Project(
                            base_point_geom::geography,
                            2.0,
                            radians(MOD((camera_heading_deg + 90.0 + 360.0)::numeric, 360.0)::double precision)
                        )::geometry,
                        4326
                    ) AS point_geom,
                    camera_heading_deg,
                    'pedestrian' AS road_category,
                    fields->>'right_sidewalk_surface_material' AS observation_surface_material,
                    'right_sidewalk_virtual' AS observation_source,
                    'right' AS observation_side
                FROM source_images
                WHERE fields->>'capture_position' = 'vehicle_road'
                    AND fields->>'right_sidewalk' = 'yes'
                    AND camera_heading_deg IS NOT NULL
                UNION ALL
                SELECT
                    image_id,
                    grid_id,
                    model,
                    prompt_version,
                    geometry,
                    image_properties,
                    fields,
                    error,
                    updated_at,
                    ST_SetSRID(
                        ST_Project(
                            base_point_geom::geography,
                            2.0,
                            radians(MOD((camera_heading_deg - 90.0 + 360.0)::numeric, 360.0)::double precision)
                        )::geometry,
                        4326
                    ) AS point_geom,
                    camera_heading_deg,
                    CASE fields->>'left_adjacent_road_type'
                        WHEN 'vehicle_road' THEN 'vehicle'
                        WHEN 'bicycle_road' THEN 'bicycle'
                        ELSE NULL
                    END AS road_category,
                    fields->>'left_adjacent_road_surface_material' AS observation_surface_material,
                    'left_adjacent_road_virtual' AS observation_source,
                    'left' AS observation_side
                FROM source_images
                WHERE fields->>'capture_position' = 'pedestrian_road'
                    AND fields->>'left_adjacent_road_type' IN ('vehicle_road', 'bicycle_road')
                    AND camera_heading_deg IS NOT NULL
                UNION ALL
                SELECT
                    image_id,
                    grid_id,
                    model,
                    prompt_version,
                    geometry,
                    image_properties,
                    fields,
                    error,
                    updated_at,
                    ST_SetSRID(
                        ST_Project(
                            base_point_geom::geography,
                            2.0,
                            radians(MOD((camera_heading_deg + 90.0 + 360.0)::numeric, 360.0)::double precision)
                        )::geometry,
                        4326
                    ) AS point_geom,
                    camera_heading_deg,
                    CASE fields->>'right_adjacent_road_type'
                        WHEN 'vehicle_road' THEN 'vehicle'
                        WHEN 'bicycle_road' THEN 'bicycle'
                        ELSE NULL
                    END AS road_category,
                    fields->>'right_adjacent_road_surface_material' AS observation_surface_material,
                    'right_adjacent_road_virtual' AS observation_source,
                    'right' AS observation_side
                FROM source_images
                WHERE fields->>'capture_position' = 'pedestrian_road'
                    AND fields->>'right_adjacent_road_type' IN ('vehicle_road', 'bicycle_road')
                    AND camera_heading_deg IS NOT NULL
            )
            SELECT
                o.image_id,
                o.grid_id,
                o.model,
                o.prompt_version,
                o.geometry,
                o.image_properties,
                o.fields,
                o.error,
                o.updated_at,
                o.point_geom,
                o.camera_heading_deg,
                o.road_category,
                s.osm_id AS selected_osm_id,
                s.name AS selected_name,
                s.highway AS selected_highway,
                s.road_category AS selected_road_category,
                s.geom_mainz AS selected_geom,
                s.osm_id AS assigned_osm_id,
                s.road_category AS assigned_road_category,
                ST_Distance(o.point_geom::geography, s.geom_mainz::geography) AS distance_m,
                ST_ClosestPoint(s.geom_mainz, o.point_geom) AS closest_point,
                s.geom_mainz AS assigned_geom,
                'side_virtual_observation' AS match_method,
                o.observation_surface_material,
                o.observation_source,
                o.observation_side,
                CASE
                    WHEN o.camera_heading_deg IS NULL THEN NULL
                    ELSE degrees(ST_Azimuth(o.point_geom::geography, ST_ClosestPoint(s.geom_mainz, o.point_geom)::geography))
                END AS target_bearing_deg,
                NULL::double precision AS view_delta_deg,
                NULL::double precision AS road_bearing_deg,
                NULL::double precision AS road_axis_delta_deg,
                true AS within_view_cone,
                ST_AsGeoJSON(o.point_geom, 6) AS point_geometry,
                ST_AsGeoJSON(ST_MakeLine(o.point_geom, ST_ClosestPoint(s.geom_mainz, o.point_geom)), 6) AS link_geometry
            FROM observations AS o
            CROSS JOIN selected AS s
            WHERE o.road_category = s.road_category
                AND o.observation_surface_material IS NOT NULL
                AND ST_DWithin(o.point_geom::geography, s.geom_mainz::geography, %s)
                AND s.osm_id = (
                    SELECT nearest_side.osm_id
                    FROM osm_roads AS nearest_side
                    WHERE {nearest_side_category_sql} = o.road_category
                    ORDER BY nearest_side.geom_mainz <-> o.point_geom
                    LIMIT 1
                )
            ORDER BY distance_m ASC
            LIMIT %s
        """
        adjacent_sidewalk_sql = f"""
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
            )
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
                'vehicle' AS road_category,
                s.osm_id AS selected_osm_id,
                s.name AS selected_name,
                s.highway AS selected_highway,
                s.road_category AS selected_road_category,
                s.geom_mainz AS selected_geom,
                s.osm_id AS assigned_osm_id,
                s.road_category AS assigned_road_category,
                ST_Distance(ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326)::geography, s.geom_mainz::geography) AS distance_m,
                ST_ClosestPoint(s.geom_mainz, ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326)) AS closest_point,
                s.geom_mainz AS assigned_geom,
                'adjacent_sidewalk_from_vehicle' AS match_method,
                CASE
                    WHEN COALESCE(
                        NULLIF(v.image_properties->>'computed_compass_angle', '')::double precision,
                        NULLIF(v.image_properties->>'compass_angle', '')::double precision
                    ) IS NULL THEN NULL
                    ELSE degrees(ST_Azimuth(
                        ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326)::geography,
                        ST_ClosestPoint(s.geom_mainz, ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326))::geography
                    ))
                END AS target_bearing_deg,
                NULL::double precision AS view_delta_deg,
                NULL::double precision AS road_bearing_deg,
                NULL::double precision AS road_axis_delta_deg,
                true AS within_view_cone,
                ST_AsGeoJSON(ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326), 6) AS point_geometry,
                ST_AsGeoJSON(
                    ST_MakeLine(
                        ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326),
                        ST_ClosestPoint(s.geom_mainz, ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326))
                    ),
                    6
                ) AS link_geometry
            FROM selected AS s
            JOIN vlm_image_analysis AS v ON true
            WHERE s.road_category = 'pedestrian'
                AND v.geometry IS NOT NULL
                AND COALESCE(v.fields->>'unusable_reason', 'none') = 'none'
                AND v.fields->>'capture_position' = 'vehicle_road'
                AND (
                    v.fields->>'left_sidewalk' = 'yes'
                    OR v.fields->>'right_sidewalk' = 'yes'
                )
                AND ST_DWithin(
                    ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326)::geography,
                    s.geom_mainz::geography,
                    %s
                )
                AND s.osm_id = (
                    SELECT nearest_ped.osm_id
                    FROM osm_roads AS nearest_ped
                    WHERE {nearest_pedestrian_category_sql} = 'pedestrian'
                    ORDER BY nearest_ped.geom_mainz <-> ST_SetSRID(ST_GeomFromGeoJSON(v.geometry::text), 4326)
                    LIMIT 1
                )
            ORDER BY distance_m ASC
            LIMIT %s
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
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
                        max_distance_m,
                        close_override_m,
                        max_distance_m,
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
                side_rows = conn.execute(
                    side_observation_sql,
                    (
                        osm_id,
                        max(max_distance_m, 60),
                        limit,
                    ),
                ).fetchall()
                rows = list(rows) + list(side_rows)
        except psycopg.Error:
            return None

        road_geometry = road_row.pop("geometry")
        road_category = road_row["road_category"]
        link_features = []
        point_features = []
        stats = {
            "capture_position": {},
            "surface_material": {},
            "matched_road_surface_material": {},
            "matched_road_surface_source": {},
            "left_adjacent_road_type": {},
            "left_adjacent_road_surface_material": {},
            "right_adjacent_road_type": {},
            "right_adjacent_road_surface_material": {},
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
            target_side = relative_side(row["camera_heading_deg"], row["target_bearing_deg"])
            matched_surface, matched_surface_source = matched_road_surface_material(
                fields,
                row["road_category"],
                row["selected_road_category"],
                target_side,
            )
            if row.get("observation_surface_material") is not None:
                matched_surface = row["observation_surface_material"]
                matched_surface_source = row.get("observation_source") or matched_surface_source
            if row["match_method"] == "adjacent_sidewalk_from_vehicle" and matched_surface is None:
                continue
            point_feature_geometry = json.loads(point_geometry)
            link_feature_geometry = json.loads(link_geometry)
            virtual_side = row.get("observation_side") or virtual_observation_side(matched_surface_source)
            virtual_geometry = None if row["match_method"] == "side_virtual_observation" else offset_point_geometry(
                point_geometry,
                row["camera_heading_deg"],
                virtual_side,
            )
            is_virtual_observation = row["match_method"] == "side_virtual_observation"
            if virtual_geometry is not None:
                point_feature_geometry = virtual_geometry
                link_feature_geometry = replace_link_start(link_geometry, virtual_geometry)
                is_virtual_observation = True
            _increment_count(stats["capture_position"], fields.get("capture_position"))
            _increment_count(stats["surface_material"], fields.get("surface_material"))
            _increment_count(stats["matched_road_surface_material"], matched_surface)
            _increment_count(stats["matched_road_surface_source"], matched_surface_source)
            _increment_count(stats["left_adjacent_road_type"], fields.get("left_adjacent_road_type"))
            _increment_count(stats["left_adjacent_road_surface_material"], fields.get("left_adjacent_road_surface_material"))
            _increment_count(stats["right_adjacent_road_type"], fields.get("right_adjacent_road_type"))
            _increment_count(stats["right_adjacent_road_surface_material"], fields.get("right_adjacent_road_surface_material"))
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
                "matched_road_surface_material": matched_surface,
                "matched_road_surface_source": matched_surface_source,
                "virtual_observation": is_virtual_observation,
                "virtual_observation_side": virtual_side,
                "observation_vote": 1,
                "target_side": target_side,
                "left_adjacent_road_type": fields.get("left_adjacent_road_type"),
                "left_adjacent_road_surface_material": fields.get("left_adjacent_road_surface_material"),
                "right_adjacent_road_type": fields.get("right_adjacent_road_type"),
                "right_adjacent_road_surface_material": fields.get("right_adjacent_road_surface_material"),
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
                    "id": f"road-vlm-link/{osm_id}/{row['image_id']}/{matched_surface_source}",
                    "geometry": link_feature_geometry,
                    "properties": properties,
                }
            )
            point_features.append(
                {
                    "type": "Feature",
                    "id": f"road-vlm-point/{osm_id}/{row['image_id']}/{matched_surface_source}",
                    "geometry": point_feature_geometry,
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

    def _unavailable_surface_validation(self, grid_id: str, reason: str) -> dict[str, Any]:
        return {
            "grid_id": grid_id,
            "available": False,
            "reason": reason,
            "layers": {"roads": {"type": "FeatureCollection", "features": []}},
            "meta": {
                "count": 0,
                "match": 0,
                "mismatch": 0,
                "skipped": {},
            },
        }
