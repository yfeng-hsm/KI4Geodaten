from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import psycopg
from psycopg.rows import dict_row


DEFAULT_DATABASE_URL = "postgresql://ki4geodaten:ki4geodaten@db:5432/ki4geodaten"
DEFAULT_OUTPUT = Path("data/graphhopper/mainz.osm.xml")
GRAPH_HOPPER_IGNORED_TAG_PREFIXES = ("oneway",)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export imported Mainz OSM roads from PostGIS as OSM XML for GraphHopper."
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows = load_roads(args.database_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_osm_xml(rows, args.output)
    print(f"Exported {len(rows)} OSM road ways to {args.output}")


def load_roads(database_url: str) -> list[dict]:
    sql = """
        SELECT
            osm_id,
            tags,
            ST_AsGeoJSON(geom, 7) AS geometry
        FROM osm_roads
        WHERE geom IS NOT NULL
        ORDER BY osm_id
    """
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        return list(conn.execute(sql))


def write_osm_xml(rows: list[dict], output: Path) -> None:
    root = ET.Element("osm", version="0.6", generator="KI4Geodaten")
    node_ids: dict[tuple[float, float], int] = {}
    next_node_id = 1

    for row in rows:
        geometry = json.loads(row["geometry"])
        for line in geometry_lines(geometry):
            for lon, lat in line:
                key = coordinate_key(lon, lat)
                if key in node_ids:
                    continue
                node_ids[key] = next_node_id
                ET.SubElement(
                    root,
                    "node",
                    id=str(next_node_id),
                    lat=f"{key[1]:.7f}",
                    lon=f"{key[0]:.7f}",
                    visible="true",
                )
                next_node_id += 1

    next_way_id = 1
    for row in rows:
        geometry = json.loads(row["geometry"])
        tags = graphhopper_tags(row["tags"] or {})
        for way_index, line in enumerate(geometry_lines(geometry)):
            if len(line) < 2:
                continue
            way = ET.SubElement(root, "way", id=str(next_way_id), visible="true")
            next_way_id += 1
            for lon, lat in line:
                ET.SubElement(way, "nd", ref=str(node_ids[coordinate_key(lon, lat)]))
            for key, value in sorted(tags.items()):
                if value is not None:
                    ET.SubElement(way, "tag", k=str(key), v=str(value))

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(output, encoding="utf-8", xml_declaration=True)


def geometry_lines(geometry: dict) -> list[list[list[float]]]:
    if geometry.get("type") == "LineString":
        return [geometry.get("coordinates") or []]
    if geometry.get("type") == "MultiLineString":
        return geometry.get("coordinates") or []
    return []


def coordinate_key(lon: float, lat: float) -> tuple[float, float]:
    return (round(float(lon), 7), round(float(lat), 7))


def graphhopper_tags(tags: dict) -> dict:
    if isinstance(tags, str):
        tags = json.loads(tags)
    return {
        key: value
        for key, value in tags.items()
        if not any(str(key).startswith(prefix) for prefix in GRAPH_HOPPER_IGNORED_TAG_PREFIXES)
    }


if __name__ == "__main__":
    main()
