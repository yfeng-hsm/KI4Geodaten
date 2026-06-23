from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import psycopg


DEFAULT_DATABASE_URL = "postgresql://ki4geodaten:ki4geodaten@db:5432/ki4geodaten"
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_BOUNDARY = Path("app/data/census/mainz_boundary.geojson")
DEFAULT_CACHE = Path("data/cache/osm_mainz_overpass.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Mainz OSM roads, buildings and landuse to PostGIS."
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--overpass-url", default=DEFAULT_OVERPASS_URL)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    boundary = json.loads(args.boundary.read_text())
    bbox = boundary_bbox(boundary)
    data = load_overpass(args.overpass_url, bbox, args.cache, args.refresh)
    nodes, ways = split_osm(data)
    roads, buildings, landuse = classify_ways(ways, nodes)

    with psycopg.connect(args.database_url) as conn:
        install_schema(conn, boundary)
        insert_rows(conn, "osm_roads", roads)
        insert_rows(conn, "osm_buildings", buildings)
        insert_rows(conn, "osm_landuse", landuse)
        clip_to_mainz(conn)
        write_meta(conn, len(roads), len(buildings), len(landuse), args.overpass_url, bbox)

    print(
        "Imported OSM ways: "
        f"{len(roads)} roads, {len(buildings)} buildings, {len(landuse)} landuse polygons"
    )


def boundary_bbox(feature_collection: dict) -> tuple[float, float, float, float]:
    coordinates = []
    for feature in geojson_features(feature_collection):
        coordinates.extend(iter_positions(feature["geometry"]["coordinates"]))
    longitudes = [position[0] for position in coordinates]
    latitudes = [position[1] for position in coordinates]
    return min(latitudes), min(longitudes), max(latitudes), max(longitudes)


def geojson_features(data: dict) -> list[dict]:
    if data["type"] == "FeatureCollection":
        return data["features"]
    if data["type"] == "Feature":
        return [data]
    return [{"type": "Feature", "geometry": data, "properties": {}}]


def iter_positions(value: list) -> Iterable[list[float]]:
    if value and isinstance(value[0], (int, float)):
        yield value
        return
    for item in value:
        yield from iter_positions(item)


def load_overpass(
    overpass_url: str,
    bbox: tuple[float, float, float, float],
    cache_path: Path,
    refresh: bool,
) -> dict:
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text())

    south, west, north, east = bbox
    bbox_text = f"{south},{west},{north},{east}"
    query = f"""
    [out:json][timeout:300];
    (
      way["highway"]["highway"!~"proposed|construction"]({bbox_text});
      way["building"]({bbox_text});
      way["landuse"]({bbox_text});
      way["natural"]({bbox_text});
      way["leisure"]({bbox_text});
      way["amenity"~"parking|school|university|hospital|marketplace|place_of_worship|grave_yard"]({bbox_text});
    );
    (._;>;);
    out body qt;
    """
    payload = query.encode("utf-8")
    request = Request(
        overpass_url,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "KI4Geodaten Mainz OSM importer",
        },
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(request, timeout=360) as response:
        body = response.read()
    cache_path.write_bytes(body)
    return json.loads(body)


def split_osm(data: dict) -> tuple[dict[int, tuple[float, float]], list[dict]]:
    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    for element in data.get("elements", []):
        if element.get("type") == "node":
            nodes[element["id"]] = (element["lon"], element["lat"])
        elif element.get("type") == "way":
            ways.append(element)
    return nodes, ways


def classify_ways(
    ways: list[dict], nodes: dict[int, tuple[float, float]]
) -> tuple[list[tuple], list[tuple], list[tuple]]:
    roads = []
    buildings = []
    landuse = []

    for way in ways:
        tags = way.get("tags", {})
        coords = [nodes[node_id] for node_id in way.get("nodes", []) if node_id in nodes]
        if len(coords) < 2:
            continue
        tags_json = json.dumps(tags, ensure_ascii=False)
        name = tags.get("name")
        osm_id = way["id"]

        if tags.get("highway") and len(coords) >= 2:
            roads.append(
                (
                    osm_id,
                    tags_json,
                    name,
                    tags.get("highway"),
                    tags.get("maxspeed"),
                    tags.get("oneway"),
                    linestring_wkt(coords),
                )
            )

        if is_closed(coords):
            if tags.get("building"):
                buildings.append(
                    (
                        osm_id,
                        tags_json,
                        name,
                        tags.get("building"),
                        polygon_wkt(coords),
                    )
                )

            kind, class_name = landuse_kind(tags)
            if kind:
                landuse.append(
                    (
                        osm_id,
                        tags_json,
                        name,
                        kind,
                        class_name,
                        polygon_wkt(coords),
                    )
                )

    return roads, buildings, landuse


def is_closed(coords: list[tuple[float, float]]) -> bool:
    return len(coords) >= 4 and coords[0] == coords[-1]


def landuse_kind(tags: dict) -> tuple[str | None, str | None]:
    for key in ("landuse", "natural", "leisure", "amenity"):
        if key in tags:
            return key, tags[key]
    return None, None


def linestring_wkt(coords: list[tuple[float, float]]) -> str:
    return "LINESTRING(" + ", ".join(format_position(coord) for coord in coords) + ")"


def polygon_wkt(coords: list[tuple[float, float]]) -> str:
    return "POLYGON((" + ", ".join(format_position(coord) for coord in coords) + "))"


def format_position(coord: tuple[float, float]) -> str:
    lon, lat = coord
    return f"{lon:.8f} {lat:.8f}"


def install_schema(conn: psycopg.Connection, boundary: dict) -> None:
    boundary_geojson = json.dumps(geojson_features(boundary)[0]["geometry"])
    conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    conn.execute("DROP TABLE IF EXISTS osm_import_meta")
    conn.execute("DROP TABLE IF EXISTS osm_roads")
    conn.execute("DROP TABLE IF EXISTS osm_buildings")
    conn.execute("DROP TABLE IF EXISTS osm_landuse")
    conn.execute("DROP TABLE IF EXISTS mainz_boundary")
    conn.execute(
        """
        CREATE TABLE mainz_boundary (
            id integer PRIMARY KEY DEFAULT 1,
            geom geometry(MultiPolygon, 4326) NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO mainz_boundary (id, geom)
        VALUES (1, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))
        """,
        (boundary_geojson,),
    )
    conn.execute(
        """
        CREATE TABLE osm_roads (
            osm_id bigint PRIMARY KEY,
            tags jsonb NOT NULL,
            name text,
            highway text,
            maxspeed text,
            oneway text,
            geom geometry(LineString, 4326) NOT NULL,
            geom_mainz geometry(MultiLineString, 4326)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE osm_buildings (
            osm_id bigint PRIMARY KEY,
            tags jsonb NOT NULL,
            name text,
            building text,
            geom geometry(Geometry, 4326) NOT NULL,
            geom_mainz geometry(MultiPolygon, 4326)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE osm_landuse (
            osm_id bigint PRIMARY KEY,
            tags jsonb NOT NULL,
            name text,
            kind text,
            class_name text,
            geom geometry(Geometry, 4326) NOT NULL,
            geom_mainz geometry(MultiPolygon, 4326)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE osm_import_meta (
            key text PRIMARY KEY,
            value jsonb NOT NULL
        )
        """
    )


def insert_rows(conn: psycopg.Connection, table: str, rows: list[tuple]) -> None:
    if table == "osm_roads":
        sql = """
            INSERT INTO osm_roads
                (osm_id, tags, name, highway, maxspeed, oneway, geom)
            VALUES (%s, %s::jsonb, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326))
            ON CONFLICT (osm_id) DO NOTHING
        """
    elif table == "osm_buildings":
        sql = """
            INSERT INTO osm_buildings
                (osm_id, tags, name, building, geom)
            VALUES (%s, %s::jsonb, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326))
            ON CONFLICT (osm_id) DO NOTHING
        """
    elif table == "osm_landuse":
        sql = """
            INSERT INTO osm_landuse
                (osm_id, tags, name, kind, class_name, geom)
            VALUES (%s, %s::jsonb, %s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326))
            ON CONFLICT (osm_id) DO NOTHING
        """
    else:
        raise ValueError(f"Unknown table {table}")

    if rows:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)


def clip_to_mainz(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        UPDATE osm_roads AS r
        SET geom_mainz = ST_Multi(ST_CollectionExtract(ST_Intersection(r.geom, b.geom), 2))
        FROM mainz_boundary AS b
        WHERE ST_Intersects(r.geom, b.geom)
        """
    )
    conn.execute("DELETE FROM osm_roads WHERE geom_mainz IS NULL OR ST_IsEmpty(geom_mainz)")

    for table in ("osm_buildings", "osm_landuse"):
        conn.execute(f"UPDATE {table} SET geom = ST_MakeValid(geom)")
        conn.execute(
            f"""
            UPDATE {table} AS f
            SET geom_mainz = ST_Multi(
                ST_CollectionExtract(ST_Intersection(f.geom, b.geom), 3)
            )
            FROM mainz_boundary AS b
            WHERE ST_Intersects(f.geom, b.geom)
            """
        )
        conn.execute(
            f"DELETE FROM {table} WHERE geom_mainz IS NULL OR ST_IsEmpty(geom_mainz)"
        )

    conn.execute("CREATE INDEX osm_roads_geom_mainz_idx ON osm_roads USING gist (geom_mainz)")
    conn.execute(
        "CREATE INDEX osm_buildings_geom_mainz_idx ON osm_buildings USING gist (geom_mainz)"
    )
    conn.execute(
        "CREATE INDEX osm_landuse_geom_mainz_idx ON osm_landuse USING gist (geom_mainz)"
    )


def write_meta(
    conn: psycopg.Connection,
    roads: int,
    buildings: int,
    landuse: int,
    overpass_url: str,
    bbox: tuple[float, float, float, float],
) -> None:
    meta = {
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "source": "OpenStreetMap Overpass API",
        "overpass_url": overpass_url,
        "bbox_south_west_north_east": bbox,
        "raw_way_counts_before_mainz_clip": {
            "roads": roads,
            "buildings": buildings,
            "landuse": landuse,
        },
        "note": "Way geometries are stored fully; API display layers are clipped per Census cell.",
    }
    conn.execute(
        "INSERT INTO osm_import_meta (key, value) VALUES ('mainz_osm_import', %s::jsonb)",
        (json.dumps(meta),),
    )


if __name__ == "__main__":
    main()
