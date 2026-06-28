from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any

import httpx

from app.config import Settings
from app.grid import CensusGridCell


MAPILLARY_IMAGES_URL = "https://graph.mapillary.com/images"
MAPILLARY_CACHE_SCHEMA_VERSION = 4
IMAGE_FIELDS = ",".join(
    [
        "id",
        "geometry",
        "computed_geometry",
        "captured_at",
        "compass_angle",
        "computed_compass_angle",
        "camera_type",
        "height",
        "width",
        "thumb_256_url",
        "thumb_1024_url",
        "sequence",
    ]
)


class MapillaryConfigurationError(RuntimeError):
    pass


class MapillaryAPIError(RuntimeError):
    pass


class MapillaryClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def images_for_cell(self, cell: CensusGridCell, refresh: bool = False) -> dict:
        cached = await asyncio.to_thread(self._read_cache, cell)
        if cached is not None and not refresh:
            cached["meta"]["cache"] = "hit"
            return cached

        if not self.settings.mapillary_access_token:
            raise MapillaryConfigurationError(
                "MAPILLARY_ACCESS_TOKEN is not configured. Copy .env.example to .env and add a token."
            )

        west, south, east, north = cell.bbox_wgs84
        params: dict[str, Any] = {
            "access_token": self.settings.mapillary_access_token,
            "bbox": f"{west},{south},{east},{north}",
            "fields": IMAGE_FIELDS,
            "limit": self.settings.max_images_per_grid,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(MAPILLARY_IMAGES_URL, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = _mapillary_error_message(exc.response)
            raise MapillaryAPIError(message) from exc
        except httpx.HTTPError as exc:
            raise MapillaryAPIError(f"Mapillary request failed: {exc}") from exc

        body = response.json()
        images = []
        for raw in body.get("data", []):
            normalized = _normalize_image(raw)
            coordinates = normalized["geometry"]["coordinates"]
            if cell.contains_wgs84(coordinates[0], coordinates[1]):
                images.append(normalized)

        result = {
            "type": "FeatureCollection",
            "features": images,
            "meta": {
                "grid_id": cell.grid_id,
                "count": len(images),
                "queried_bbox_count": len(body.get("data", [])),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "cache": "miss",
                "truncated": len(body.get("data", []))
                >= self.settings.max_images_per_grid,
                "mapillary_cache_schema_version": MAPILLARY_CACHE_SCHEMA_VERSION,
            },
        }
        await asyncio.to_thread(self._write_cache, cell, result)
        return result

    def _cache_path(self, cell: CensusGridCell) -> Path:
        return self.settings.cache_dir / f"{cell.grid_id}.geojson"

    def _read_cache(self, cell: CensusGridCell) -> dict | None:
        path = self._cache_path(cell)
        if not path.exists():
            return None
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        if age > self.settings.cache_ttl_seconds:
            return None
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if cached.get("meta", {}).get("mapillary_cache_schema_version") != MAPILLARY_CACHE_SCHEMA_VERSION:
            return None
        return cached

    def _write_cache(self, cell: CensusGridCell, result: dict) -> None:
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        destination = self._cache_path(cell)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.settings.cache_dir,
            delete=False,
        ) as handle:
            json.dump(result, handle, ensure_ascii=False)
            temporary = Path(handle.name)
        temporary.replace(destination)

    def cached_image_index(self) -> dict[str, dict[str, Any]]:
        if not self.settings.cache_dir.exists():
            return {}
        index = {}
        for collection in self._cached_collections():
            for feature in collection.get("features", []):
                image_id = str(feature.get("id") or feature.get("properties", {}).get("id") or "")
                if image_id:
                    index[image_id] = feature
        return index

    def cached_images_for_sequence(self, sequence_id: str) -> dict:
        features_by_id: dict[str, dict[str, Any]] = {}
        grid_ids: set[str] = set()
        for collection in self._cached_collections():
            grid_id = collection.get("meta", {}).get("grid_id")
            for feature in collection.get("features", []):
                properties = feature.get("properties", {})
                if str(properties.get("sequence_id") or "") != sequence_id:
                    continue
                image_id = str(feature.get("id") or properties.get("id") or "")
                if not image_id:
                    continue
                features_by_id[image_id] = feature
                if grid_id:
                    grid_ids.add(str(grid_id))
        features = sorted(
            features_by_id.values(),
            key=lambda item: (
                item.get("properties", {}).get("captured_at") or 0,
                str(item.get("id") or item.get("properties", {}).get("id") or ""),
            ),
        )
        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "sequence_id": sequence_id,
                "count": len(features),
                "grid_count": len(grid_ids),
                "cache": "sequence-scan",
            },
        }

    def cached_images_for_cell(self, cell: CensusGridCell) -> dict | None:
        path = self._cache_path(cell)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _cached_collections(self) -> list[dict]:
        if not self.settings.cache_dir.exists():
            return []
        collections = []
        for path in self.settings.cache_dir.glob("*.geojson"):
            try:
                collection = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if collection.get("meta", {}).get("mapillary_cache_schema_version") != MAPILLARY_CACHE_SCHEMA_VERSION:
                continue
            collections.append(collection)
        return collections


def _normalize_image(raw: dict) -> dict:
    original_geometry = raw.get("geometry")
    computed_geometry = raw.get("computed_geometry")
    geometry = original_geometry or computed_geometry
    properties = {key: value for key, value in raw.items() if key not in {"geometry", "computed_geometry"}}
    properties["original_geometry"] = original_geometry
    properties["computed_geometry"] = computed_geometry
    properties["geometry_source"] = "original" if original_geometry else "computed"
    sequence = properties.get("sequence")
    if isinstance(sequence, dict):
        properties["sequence_id"] = sequence.get("id")
        del properties["sequence"]
    elif sequence:
        properties["sequence_id"] = str(sequence)
        del properties["sequence"]
    properties["mapillary_url"] = f"https://www.mapillary.com/app/?pKey={raw['id']}"
    return {
        "type": "Feature",
        "id": raw["id"],
        "geometry": geometry,
        "properties": properties,
    }


def _mapillary_error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
        detail = error.get("message") or response.text
    except (ValueError, AttributeError):
        detail = response.text
    return f"Mapillary API returned HTTP {response.status_code}: {detail}"
