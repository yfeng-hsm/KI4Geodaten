from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.grid import cell_for_point
from app.mapillary import MapillaryClient, MapillaryConfigurationError, _normalize_image


def settings(tmp_path: Path, token: str | None = None) -> Settings:
    return Settings(
        mapillary_access_token=token,
        cache_dir=tmp_path,
        cache_ttl_seconds=3600,
        max_images_per_grid=2000,
        database_url=None,
        ollama_base_url=None,
        ollama_model="gemma4:31b",
        ollama_timeout_seconds=180,
        ollama_max_images_per_request=20,
        ollama_image_thumb_size=256,
    )


@pytest.mark.asyncio
async def test_missing_token_has_actionable_error(tmp_path):
    client = MapillaryClient(settings(tmp_path))
    cell = cell_for_point(13.4095, 52.5208)

    with pytest.raises(MapillaryConfigurationError, match="MAPILLARY_ACCESS_TOKEN"):
        await client.images_for_cell(cell)


@pytest.mark.asyncio
async def test_fetch_filters_bbox_results_to_exact_grid(tmp_path, monkeypatch):
    cell = cell_for_point(13.4095, 52.5208)
    inside = cell.ring_wgs84[0]
    inside = [
        (inside[0] + cell.ring_wgs84[2][0]) / 2,
        (inside[1] + cell.ring_wgs84[2][1]) / 2,
    ]
    outside = [cell.bbox_wgs84[2] + 0.001, cell.bbox_wgs84[3] + 0.001]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "data": [
                    {"id": "inside", "geometry": {"type": "Point", "coordinates": inside}},
                    {"id": "outside", "geometry": {"type": "Point", "coordinates": outside}},
                ]
            },
        )

    original_client = httpx.AsyncClient

    class MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    try:
        result = await MapillaryClient(settings(tmp_path, "token")).images_for_cell(cell)
    finally:
        monkeypatch.setattr(httpx, "AsyncClient", original_client)

    assert result["meta"]["queried_bbox_count"] == 2
    assert result["meta"]["count"] == 1
    assert [feature["id"] for feature in result["features"]] == ["inside"]


def test_normalize_preserves_original_and_computed_geometry():
    feature = _normalize_image(
        {
            "id": "123",
            "geometry": {"type": "Point", "coordinates": [1, 2]},
            "computed_geometry": {"type": "Point", "coordinates": [3, 4]},
            "sequence": {"id": "sequence-1"},
        }
    )

    assert feature["geometry"]["coordinates"] == [1, 2]
    assert feature["properties"]["original_geometry"]["coordinates"] == [1, 2]
    assert feature["properties"]["computed_geometry"]["coordinates"] == [3, 4]
    assert feature["properties"]["geometry_source"] == "original"
    assert feature["properties"]["sequence_id"] == "sequence-1"
    assert feature["properties"]["mapillary_url"].endswith("pKey=123")
