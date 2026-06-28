import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import math
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

from app.census import CensusStore
from app.config import Settings
from app.grid import cell_for_point, cell_from_id, neighboring_cells
from app.graphhopper import GraphHopperClient
from app.mapillary import (
    MapillaryAPIError,
    MapillaryClient,
    MapillaryConfigurationError,
)
from app.mapillary_positions import MapillaryPositionStore
from app.ollama import OllamaClient, OllamaConfigurationError
from app.osm import OSMStore
from app.vlm_store import VLMAnalysisStore


STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    app.state.settings = settings
    app.state.mapillary = MapillaryClient(settings)
    app.state.mapillary_positions = MapillaryPositionStore(settings.database_url)
    app.state.graphhopper = GraphHopperClient(settings)
    app.state.census = CensusStore(Path(__file__).parent / "data" / "census")
    app.state.osm = OSMStore(settings.database_url)
    app.state.ollama = OllamaClient(settings)
    app.state.vlm_store = VLMAnalysisStore(settings.database_url)
    app.state.mapillary_positions.ensure_schema()
    app.state.vlm_store.ensure_schema()
    app.state.vlm_store.migrate_geometry_to_mapillary_computed()
    app.state.vlm_store.recover_interrupted_jobs()
    app.state.vlm_queue_event = asyncio.Event()
    app.state.vlm_worker_task = asyncio.create_task(_vlm_queue_worker(app))
    try:
        yield
    finally:
        app.state.vlm_worker_task.cancel()
        try:
            await app.state.vlm_worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="KI4Geodaten Mapillary Grid Explorer",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    return {
        "status": "ok",
        "mapillary_configured": bool(settings.mapillary_access_token),
        "ollama": await request.app.state.ollama.status(),
        "graphhopper": await request.app.state.graphhopper.status(),
        "grid_crs": "EPSG:3035",
        "grid_resolution_m": 100,
        "census_city": request.app.state.census.meta["city"],
        "census_cell_count": request.app.state.census.meta["cell_count"],
        "osm_database": request.app.state.osm.status(),
    }


@app.get("/api/ollama/status")
async def ollama_status(request: Request) -> dict:
    return await request.app.state.ollama.status()


@app.get("/api/mainz")
async def mainz_metadata(request: Request) -> dict:
    census: CensusStore = request.app.state.census
    return {"meta": census.meta, "boundary": census.boundary}


@app.get("/api/mainz/grids")
async def mainz_grids(request: Request) -> JSONResponse:
    census: CensusStore = request.app.state.census
    return JSONResponse(census.dataset)


@app.get("/api/mainz/grids/{grid_id}")
async def mainz_grid(grid_id: str, request: Request) -> dict:
    census: CensusStore = request.app.state.census
    cell = census.cell(grid_id)
    if cell is None:
        raise HTTPException(status_code=404, detail="Grid cell is not in Mainz")
    return cell


@app.get("/api/mainz/grids/{grid_id}/map-layers")
async def mainz_grid_map_layers(
    grid_id: str,
    request: Request,
    radius: int = Query(default=0, ge=0, le=10),
) -> dict:
    census: CensusStore = request.app.state.census
    census_cell = census.cell(grid_id)
    if census_cell is None:
        raise HTTPException(status_code=404, detail="Grid cell is not in Mainz")
    try:
        cell = cell_from_id(grid_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    clip_feature = cell.neighborhood_feature(radius) if radius else cell.to_feature()
    return request.app.state.osm.cell_layers(
        grid_id=grid_id,
        cell_geometry=clip_feature["geometry"],
    )


@app.get("/api/mainz/grids/{grid_id}/road-surface-validation")
async def mainz_grid_road_surface_validation(grid_id: str, request: Request) -> dict:
    census: CensusStore = request.app.state.census
    if census.cell(grid_id) is None:
        raise HTTPException(status_code=404, detail="Grid cell is not in Mainz")
    try:
        cell = cell_from_id(grid_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return request.app.state.osm.cell_road_surface_validation(
        grid_id=grid_id,
        cell_geometry=cell.to_feature()["geometry"],
    )


@app.get("/api/mainz/road-surface-validation")
async def mainz_road_surface_validation(request: Request) -> dict:
    return request.app.state.osm.cell_road_surface_validation(
        grid_id="mainz",
        cell_geometry=None,
    )


@app.get("/api/osm/roads/{osm_id}")
async def osm_road(osm_id: int, request: Request) -> dict:
    road = request.app.state.osm.road(osm_id)
    if road is None:
        raise HTTPException(status_code=404, detail="OSM road is not available")
    return road


@app.get("/api/osm/roads/{osm_id}/vlm-matches")
async def osm_road_vlm_matches(
    osm_id: int,
    request: Request,
    max_distance_m: float = Query(default=8, ge=1, le=200),
    close_override_m: float = Query(default=4, ge=0, le=50),
    view_fov_deg: float = Query(default=110, ge=30, le=180),
    on_road_visible_m: float = Query(default=1, ge=0, le=10),
    no_heading_visible_m: float = Query(default=3, ge=0, le=20),
    road_axis_tolerance_deg: float = Query(default=35, ge=0, le=90),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    matches = request.app.state.osm.road_vlm_matches(
        osm_id,
        max_distance_m=max_distance_m,
        close_override_m=close_override_m,
        view_fov_deg=view_fov_deg,
        on_road_visible_m=on_road_visible_m,
        no_heading_visible_m=no_heading_visible_m,
        road_axis_tolerance_deg=road_axis_tolerance_deg,
        limit=limit,
    )
    if matches is None:
        raise HTTPException(status_code=404, detail="OSM road VLM matches are not available")
    return matches


@app.post("/api/vlm/analyze-image")
async def analyze_image(payload: dict, request: Request) -> dict:
    grid_id = str(payload.get("grid_id", ""))
    image = payload.get("image")
    if not grid_id:
        raise HTTPException(status_code=422, detail="grid_id is required")
    if not isinstance(image, dict):
        raise HTTPException(status_code=422, detail="image feature is required")

    try:
        result = await request.app.state.ollama.analyze_images(
            grid_id,
            [image],
            model=payload.get("model"),
        )
    except OllamaConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result["results"]:
        analysis = result["results"][0]
        if analysis.get("ok") and not analysis.get("error"):
            stored = request.app.state.vlm_store.upsert(grid_id, analysis)
            result["stored"] = stored
    return result


@app.get("/api/grids/{grid_id}/vlm-results")
async def vlm_results_for_grid(grid_id: str, request: Request) -> dict:
    result = request.app.state.vlm_store.results_for_grid(grid_id)
    return _enrich_vlm_results_from_cache(result, request.app.state.mapillary.cached_image_index())


@app.get("/api/grids/{grid_id}/map-matching")
async def map_matching_for_grid(
    grid_id: str,
    request: Request,
    limit: int = Query(default=500, ge=2, le=1000),
    sequence_id: str | None = Query(default=None),
    max_gap_m: float = Query(default=40.0, ge=5.0, le=500.0),
) -> dict:
    try:
        cell = cell_from_id(grid_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cached = request.app.state.mapillary.cached_images_for_cell(cell)
    if cached is None:
        return {
            "grid_id": grid_id,
            "available": False,
            "reason": "Mapillary images are not cached for this cell",
            "layers": _empty_map_matching_layers(),
            "meta": {"count": 0},
        }

    cell_vlm_results = request.app.state.vlm_store.results_for_grid(grid_id).get("results", {})
    cell_observations = _map_matching_observations(cached.get("features", []), cell_vlm_results)
    selected_sequence_id = _select_mapillary_sequence_id(cell_observations, sequence_id=sequence_id)
    if selected_sequence_id:
        sequence_cached = request.app.state.mapillary.cached_images_for_sequence(selected_sequence_id)
        vlm_results = request.app.state.vlm_store.all_results(limit=50000).get("results", {})
        observations = _map_matching_observations(sequence_cached.get("features", []), vlm_results)
    else:
        sequence_cached = None
        observations = cell_observations
    selected_sequence = _select_mapillary_sequence(
        observations,
        sequence_id=selected_sequence_id or sequence_id,
        limit=limit,
    )
    if selected_sequence is None:
        return {
            "grid_id": grid_id,
            "available": False,
            "reason": "No real Mapillary sequence_id is available in this cell cache. Re-confirm Mapillary access to refresh metadata; random cell-level time sorting is intentionally disabled.",
            "layers": _empty_map_matching_layers(),
            "meta": {
                "count": len(cell_observations),
                "matched": 0,
                "method": "graphhopper_sequence_map_matching",
            },
        }
    filtered_sequence, stop_cluster_count, stop_point_count = _drop_stop_clusters(selected_sequence)
    if len(filtered_sequence) >= 2:
        selected_sequence = filtered_sequence
    else:
        stop_cluster_count = 0
        stop_point_count = 0
    segments = _split_map_matching_segments(selected_sequence, max_gap_m=max_gap_m)
    result = await _graphhopper_map_matching_segment_layers(
        segments,
        request.app.state.graphhopper,
        request.app.state.osm,
        sequence_id=str(selected_sequence[0].get("sequence_id") or "unknown"),
        max_gap_m=max_gap_m,
    )
    result["grid_id"] = grid_id
    result["meta"]["cell_count"] = len(cell_observations)
    result["meta"]["stop_cluster_count"] = stop_cluster_count
    result["meta"]["stop_point_count"] = stop_point_count
    if sequence_cached is not None:
        result["meta"]["sequence_cached_count"] = sequence_cached.get("meta", {}).get("count", len(observations))
        result["meta"]["sequence_cached_grid_count"] = sequence_cached.get("meta", {}).get("grid_count")
    return result


@app.get("/api/mapillary/mapmatched-positions")
async def all_mapmatched_positions(
    request: Request,
    limit: int = Query(default=50000, ge=1, le=100000),
) -> dict:
    result = request.app.state.mapillary_positions.all_features(limit=limit)
    return _enrich_mapmatched_position_features(result, request.app.state.mapillary.cached_image_index())


@app.get("/api/grids/{grid_id}/mapmatched-positions")
async def grid_mapmatched_positions(grid_id: str, request: Request) -> dict:
    result = request.app.state.mapillary_positions.features_for_grid(grid_id)
    return _enrich_mapmatched_position_features(result, request.app.state.mapillary.cached_image_index())


@app.post("/api/grids/{grid_id}/mapmatched-positions")
async def save_grid_mapmatched_positions(grid_id: str, payload: dict, request: Request) -> dict:
    if not request.app.state.mapillary_positions.configured:
        raise HTTPException(status_code=503, detail="DATABASE_URL is required for map-matched positions")
    features = payload.get("features") or payload.get("images")
    if not isinstance(features, list) or not features:
        raise HTTPException(status_code=422, detail="features list is required")
    result = request.app.state.mapillary_positions.upsert_many(grid_id, features)
    return _enrich_mapmatched_position_features(result, request.app.state.mapillary.cached_image_index())


@app.get("/api/vlm-results")
async def all_vlm_results(
    request: Request,
    limit: int = Query(default=5000, ge=1, le=50000),
) -> dict:
    result = request.app.state.vlm_store.all_results(limit=limit)
    return _enrich_vlm_results_from_cache(result, request.app.state.mapillary.cached_image_index())


@app.delete("/api/vlm-results/{image_id}")
async def delete_vlm_result(image_id: str, request: Request) -> dict:
    if not request.app.state.vlm_store.configured:
        raise HTTPException(status_code=503, detail="DATABASE_URL is required for VLM results")
    deleted = request.app.state.vlm_store.delete_result(image_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="VLM result not found")
    return {"image_id": image_id, "deleted": True}


@app.delete("/api/grids/{grid_id}/vlm-results")
async def delete_vlm_results_for_grid(grid_id: str, request: Request) -> dict:
    if not request.app.state.vlm_store.configured:
        raise HTTPException(status_code=503, detail="DATABASE_URL is required for VLM results")
    deleted = request.app.state.vlm_store.delete_results_for_grid(grid_id)
    return {"grid_id": grid_id, "deleted": deleted}


@app.post("/api/grids/{grid_id}/vlm-jobs")
async def start_vlm_job(grid_id: str, payload: dict, request: Request) -> dict:
    if not request.app.state.vlm_store.configured:
        raise HTTPException(status_code=503, detail="DATABASE_URL is required for VLM job queue")
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        raise HTTPException(status_code=422, detail="images list is required")

    model = str(payload.get("model") or request.app.state.settings.ollama_model)
    force = bool(payload.get("force") or payload.get("overwrite_existing"))
    job_id = uuid4().hex
    job = request.app.state.vlm_store.create_job(job_id, grid_id, model, force, images)
    request.app.state.vlm_queue_event.set()
    return job


@app.get("/api/vlm/jobs")
async def vlm_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return request.app.state.vlm_store.list_jobs(limit=limit)


@app.get("/api/vlm/jobs/{job_id}")
async def vlm_job_status(job_id: str, request: Request) -> dict:
    job = request.app.state.vlm_store.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="VLM job not found")
    return job


@app.post("/api/vlm/jobs/{job_id}/cancel")
async def cancel_vlm_job(job_id: str, request: Request) -> dict:
    job = request.app.state.vlm_store.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="VLM job not found")
    request.app.state.vlm_queue_event.set()
    return job


async def _vlm_queue_worker(app: FastAPI) -> None:
    while True:
        job = await asyncio.to_thread(app.state.vlm_store.claim_next_job)
        if job is None:
            try:
                await asyncio.wait_for(app.state.vlm_queue_event.wait(), timeout=2)
                app.state.vlm_queue_event.clear()
            except asyncio.TimeoutError:
                pass
            continue
        await _run_vlm_job(app, job)


async def _run_vlm_job(app: FastAPI, job: dict) -> None:
    job_id = job["job_id"]
    grid_id = job["grid_id"]
    model = job["model"]
    force = job["force"]
    images = [item["image"] for item in job["items"]]
    processed = job["processed"]
    analyzed = job["analyzed"]
    skipped = job["skipped"]
    failed = job["failed"]
    analysis_seconds_sum = float(job.get("analysis_seconds_sum") or 0)
    concurrency = max(1, int(app.state.settings.ollama_concurrency or 1))

    async def process_image(client: httpx.AsyncClient, image: dict) -> dict:
        image_id = _image_id(image)
        app.state.vlm_store.update_job_item(job_id, image_id, "running")
        if image_id in existing_ids:
            app.state.vlm_store.update_image_metadata(grid_id, image)
            app.state.vlm_store.update_job_item(job_id, image_id, "skipped")
            return {
                "processed": 0,
                "analyzed": 0,
                "skipped": 1,
                "failed": 0,
                "analysis_seconds": 0.0,
                "stored_image_id": None,
            }

        started_at = time.perf_counter()
        result = await app.state.ollama.analyze_one(client, image, model)
        elapsed_seconds = time.perf_counter() - started_at
        error = result.get("error")
        failed_delta = 1 if error or not result.get("ok") else 0
        stored = None if failed_delta else app.state.vlm_store.upsert(grid_id, result)
        app.state.vlm_store.update_job_item(
            job_id,
            image_id,
            "failed" if failed_delta else "completed",
            error,
        )
        return {
            "processed": 1,
            "analyzed": 1,
            "skipped": 0,
            "failed": failed_delta,
            "analysis_seconds": 0.0 if failed_delta else elapsed_seconds,
            "stored_image_id": stored["image_id"] if stored else None,
        }

    try:
        image_ids = [_image_id(image) for image in images]
        existing_ids = set() if force else app.state.vlm_store.existing_image_ids(image_ids)
        async with httpx.AsyncClient(timeout=app.state.settings.ollama_timeout_seconds) as client:
            for start in range(0, len(images), concurrency):
                if app.state.vlm_store.should_cancel_job(job_id):
                    app.state.vlm_store.update_job(
                        job_id,
                        status="cancelled",
                        completed_at=datetime.now(timezone.utc),
                        current_image_id=None,
                    )
                    return
                batch = images[start : start + concurrency]
                running_ids = [_image_id(image) for image in batch]
                app.state.vlm_store.update_job(
                    job_id,
                    current_image_id=", ".join(running_ids),
                    processed=processed,
                    analyzed=analyzed,
                    skipped=skipped,
                    failed=failed,
                    analysis_seconds_sum=analysis_seconds_sum,
                )
                results = await asyncio.gather(
                    *(process_image(client, image) for image in batch)
                )
                last_stored_image_id = None
                for result in results:
                    processed += result["processed"]
                    analyzed += result["analyzed"]
                    skipped += result["skipped"]
                    failed += result["failed"]
                    analysis_seconds_sum += result["analysis_seconds"]
                    last_stored_image_id = result["stored_image_id"] or last_stored_image_id
                app.state.vlm_store.update_job(
                    job_id,
                    processed=processed,
                    analyzed=analyzed,
                    skipped=skipped,
                    failed=failed,
                    analysis_seconds_sum=analysis_seconds_sum,
                    current_image_id=None,
                    last_stored_image_id=last_stored_image_id,
                )
                if app.state.vlm_store.should_cancel_job(job_id):
                    app.state.vlm_store.update_job(
                        job_id,
                        status="cancelled",
                        completed_at=datetime.now(timezone.utc),
                        current_image_id=None,
                    )
                    return
        app.state.vlm_store.update_job(
            job_id,
            status="completed",
            completed_at=datetime.now(timezone.utc),
            current_image_id=None,
        )
    except Exception as exc:  # pragma: no cover - defensive job boundary
        app.state.vlm_store.update_job(
            job_id,
            status="failed",
            error=f"{exc.__class__.__name__}: {exc}",
            current_image_id=None,
        )


def _image_id(image: dict) -> str:
    return str(image.get("id") or image.get("properties", {}).get("id") or "")


def _enrich_vlm_results_from_cache(result: dict, cached_images: dict[str, dict]) -> dict:
    for image_id, analysis in result.get("results", {}).items():
        cached = cached_images.get(str(image_id))
        if not cached:
            analysis["geometry"] = analysis.get("geometry") or _cell_center_geometry(
                analysis.get("grid_id", "")
            )
            continue
        analysis["geometry"] = analysis.get("geometry") or cached.get("geometry")
        cached_properties = cached.get("properties") or {}
        image_properties = analysis.get("image_properties") or {}
        analysis["image_properties"] = {**cached_properties, **image_properties}
        analysis["geometry"] = analysis.get("geometry") or _cell_center_geometry(
            analysis.get("grid_id", "")
        )
    return result


def _enrich_mapmatched_position_features(result: dict, cached_images: dict[str, dict]) -> dict:
    features = []
    for feature in result.get("features", []):
        image_id = str(feature.get("id") or feature.get("properties", {}).get("image_id") or "")
        cached = cached_images.get(image_id)
        properties = dict(feature.get("properties") or {})
        if cached:
            cached_properties = cached.get("properties") or {}
            properties = {**cached_properties, **properties}
            properties["original_geometry"] = properties.get("gps_geometry") or cached_properties.get("original_geometry")
            properties["computed_geometry"] = properties.get("mapillary_geometry") or cached_properties.get("computed_geometry")
        features.append(
            {
                **feature,
                "id": image_id or feature.get("id"),
                "properties": properties,
            }
        )
    result["features"] = features
    result["geojson"] = {"type": "FeatureCollection", "features": features}
    return result


def _empty_map_matching_layers() -> dict:
    return {
        "points": {"type": "FeatureCollection", "features": []},
        "links": {"type": "FeatureCollection", "features": []},
        "matched_roads": {"type": "FeatureCollection", "features": []},
        "raw_trajectory": {"type": "FeatureCollection", "features": []},
        "matched_trajectory": {"type": "FeatureCollection", "features": []},
    }


def _map_matching_observations(features: list[dict], vlm_results: dict) -> list[dict]:
    rows = []
    for feature in features:
        image_id = str(feature.get("id") or feature.get("properties", {}).get("id") or "")
        properties = feature.get("properties", {})
        gps_geometry = properties.get("original_geometry") or feature.get("geometry")
        mapillary_geometry = properties.get("computed_geometry") or feature.get("geometry")
        coordinates = gps_geometry.get("coordinates") if isinstance(gps_geometry, dict) else None
        if not image_id or not coordinates or len(coordinates) < 2:
            continue
        vlm = vlm_results.get(image_id) or {}
        fields = vlm.get("fields") or {}
        rows.append(
            {
                "image_id": image_id,
                "captured_at": properties.get("captured_at") or 0,
                "sequence_id": properties.get("sequence_id"),
                "lon": float(coordinates[0]),
                "lat": float(coordinates[1]),
                "gps_geometry": gps_geometry,
                "mapillary_geometry": mapillary_geometry,
                "heading_deg": _optional_float(
                    properties.get("computed_compass_angle") or properties.get("compass_angle")
                ),
                "capture_position": fields.get("capture_position"),
                "surface_material": fields.get("surface_material"),
                "thumb_256_url": properties.get("thumb_256_url"),
                "thumb_1024_url": properties.get("thumb_1024_url"),
                "mapillary_url": properties.get("mapillary_url"),
                "camera_type": properties.get("camera_type"),
                "width": properties.get("width"),
                "height": properties.get("height"),
            }
        )
    rows.sort(key=lambda row: (row.get("sequence_id") or "", row["captured_at"], row["image_id"]))
    for index, row in enumerate(rows):
        previous_row = _compatible_track_neighbor(row, rows[index - 1]) if index > 0 else None
        next_row = _compatible_track_neighbor(row, rows[index + 1]) if index + 1 < len(rows) else None
        bearing_start = previous_row or row
        bearing_end = next_row or row
        row["idx"] = index
        row["track_heading_deg"] = _bearing_degrees(
            bearing_start["lon"],
            bearing_start["lat"],
            bearing_end["lon"],
            bearing_end["lat"],
        )
    return rows


def _select_mapillary_sequence(
    observations: list[dict],
    *,
    sequence_id: str | None,
    limit: int,
) -> list[dict] | None:
    groups: dict[str, list[dict]] = {}
    for observation in observations:
        current_sequence = observation.get("sequence_id")
        if not current_sequence:
            continue
        groups.setdefault(str(current_sequence), []).append(observation)
    if not groups:
        return None
    if sequence_id:
        selected = groups.get(sequence_id)
        return selected[:limit] if selected and len(selected) >= 2 else None
    selected = max(groups.values(), key=len)
    return selected[:limit] if len(selected) >= 2 else None


def _select_mapillary_sequence_id(observations: list[dict], *, sequence_id: str | None) -> str | None:
    groups: dict[str, int] = {}
    for observation in observations:
        current_sequence = observation.get("sequence_id")
        if not current_sequence:
            continue
        groups[str(current_sequence)] = groups.get(str(current_sequence), 0) + 1
    if sequence_id:
        return sequence_id if groups.get(sequence_id, 0) >= 2 else None
    if not groups:
        return None
    return sorted(groups.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _split_map_matching_segments(observations: list[dict], *, max_gap_m: float) -> list[list[dict]]:
    segments: list[list[dict]] = []
    current: list[dict] = []
    for observation in observations:
        if current:
            previous = current[-1]
            gap_m = _haversine_m(previous["lon"], previous["lat"], observation["lon"], observation["lat"])
            if gap_m > max_gap_m:
                segments.append(current)
                current = []
        current.append(observation)
    if current:
        segments.append(current)
    return segments


def _drop_stop_clusters(
    observations: list[dict],
    *,
    radius_m: float = 5.0,
    min_points: int = 6,
) -> tuple[list[dict], int, int]:
    if len(observations) < min_points:
        return observations, 0, 0
    stop_indexes: set[int] = set()
    index = 0
    clusters = 0
    while index < len(observations):
        anchor = observations[index]
        cluster_indexes = [index]
        cursor = index + 1
        while cursor < len(observations):
            candidate = observations[cursor]
            if _haversine_m(anchor["lon"], anchor["lat"], candidate["lon"], candidate["lat"]) > radius_m:
                break
            cluster_indexes.append(cursor)
            cursor += 1
        if len(cluster_indexes) >= min_points:
            stop_indexes.update(cluster_indexes)
            clusters += 1
            index = cursor
        else:
            index += 1
    if not stop_indexes:
        return observations, 0, 0
    filtered = [row for idx, row in enumerate(observations) if idx not in stop_indexes]
    return _reindex_map_matching_observations(filtered), clusters, len(stop_indexes)


def _reindex_map_matching_observations(observations: list[dict]) -> list[dict]:
    rows = list(observations)
    for index, row in enumerate(rows):
        previous_row = _compatible_track_neighbor(row, rows[index - 1]) if index > 0 else None
        next_row = _compatible_track_neighbor(row, rows[index + 1]) if index + 1 < len(rows) else None
        bearing_start = previous_row or row
        bearing_end = next_row or row
        row["idx"] = index
        row["track_heading_deg"] = _bearing_degrees(
            bearing_start["lon"],
            bearing_start["lat"],
            bearing_end["lon"],
            bearing_end["lat"],
        )
    return rows


async def _graphhopper_map_matching_segment_layers(
    segments: list[list[dict]],
    graphhopper: GraphHopperClient,
    osm_store: OSMStore,
    *,
    sequence_id: str,
    max_gap_m: float,
) -> dict:
    aggregate_layers = _empty_map_matching_layers()
    reasons: list[str] = []
    matched_count = 0
    total_count = 0
    matched_distance = 0.0
    matched_time = 0
    matched_segments = 0
    segment_summaries: list[dict] = []

    for segment_index, segment in enumerate(segments):
        total_count += len(segment)
        if len(segment) < 2:
            aggregate_layers["points"]["features"].extend(
                _map_matching_point_feature(observation, None, segment_index=segment_index)
                for observation in segment
            )
            segment_summaries.append(
                {
                    "segment_index": segment_index,
                    "count": len(segment),
                    "matched": 0,
                    "available": False,
                    "profile": None,
                    "distance": None,
                    "start_captured_at": segment[0].get("captured_at") if segment else None,
                    "end_captured_at": segment[-1].get("captured_at") if segment else None,
                }
            )
            continue

        profile, graphhopper_result = await _best_graphhopper_result(segment, graphhopper)
        segment_result = _graphhopper_map_matching_layers(
            segment,
            graphhopper_result,
            profile=profile,
            osm_store=osm_store,
            segment_index=segment_index,
            max_gap_m=max_gap_m,
        )
        for layer_name, layer in segment_result["layers"].items():
            aggregate_layers[layer_name]["features"].extend(layer.get("features", []))
        matched_count += int(segment_result.get("meta", {}).get("matched") or 0)
        if segment_result.get("available"):
            matched_segments += 1
            if segment_result.get("meta", {}).get("distance") is not None:
                matched_distance += float(segment_result["meta"]["distance"])
            if segment_result.get("meta", {}).get("time") is not None:
                matched_time += int(segment_result["meta"]["time"])
        else:
            reasons.append(str(segment_result.get("reason") or "segment matching failed"))
        segment_meta = segment_result.get("meta", {})
        segment_summaries.append(
            {
                "segment_index": segment_index,
                "count": len(segment),
                "matched": int(segment_meta.get("matched") or 0),
                "available": bool(segment_result.get("available")),
                "profile": segment_meta.get("profile"),
                "distance": segment_meta.get("distance"),
                "start_captured_at": segment[0].get("captured_at") if segment else None,
                "end_captured_at": segment[-1].get("captured_at") if segment else None,
            }
        )

    available = matched_segments > 0
    return {
        "available": available,
        "reason": "; ".join(reasons[:3]) if reasons and not available else None,
        "layers": aggregate_layers,
        "meta": {
            "count": total_count,
            "matched": matched_count,
            "sequence_id": sequence_id,
            "profile": "segmented",
            "distance": matched_distance if available else None,
            "time": matched_time if available else None,
            "segments": len(segments),
            "matched_segments": matched_segments,
            "segment_summaries": segment_summaries,
            "max_gap_m": max_gap_m,
            "method": "graphhopper_sequence_map_matching",
        },
    }


async def _best_graphhopper_result(
    observations: list[dict],
    graphhopper: GraphHopperClient,
) -> tuple[str, dict]:
    candidates: list[tuple[float, str, dict]] = []
    failures: list[str] = []
    for profile in ("foot", "bike", "car"):
        result = await graphhopper.match_trace(observations, profile=profile)
        if not result.get("available"):
            failures.append(f"{profile}: {result.get('reason', 'unavailable')}")
            continue
        score = _mean_distance_to_matched_geometry(observations, result.get("geometry") or {})
        candidates.append((score, profile, result))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        selected_score, selected_profile, selected_result = candidates[0]
        selected_result["profile_scores"] = {
            profile: round(score, 3)
            for score, profile, _ in candidates
        }
        selected_result["profile_score"] = round(selected_score, 3)
        return selected_profile, selected_result
    return "auto", {"available": False, "reason": "; ".join(failures[:3]) or "No GraphHopper profile matched"}


def _mean_distance_to_matched_geometry(observations: list[dict], geometry: dict) -> float:
    snapped = _nearest_points_on_linestring(observations, geometry)
    if not snapped:
        return float("inf")
    distances = [
        _haversine_m(row["lon"], row["lat"], point[0], point[1])
        for row, point in zip(observations, snapped)
    ]
    return sum(distances) / len(distances) if distances else float("inf")


def _graphhopper_map_matching_layers(
    observations: list[dict],
    graphhopper_result: dict,
    *,
    profile: str,
    osm_store: OSMStore | None = None,
    segment_index: int = 0,
    max_gap_m: float = 40.0,
) -> dict:
    sequence_id = str(observations[0].get("sequence_id") or "unknown")
    raw_points = [
        _map_matching_point_feature(observation, None, segment_index=segment_index)
        for observation in observations
    ]
    raw_trajectory = _line_feature(
        f"mapillary-sequence-raw/{segment_index}",
        [[row["lon"], row["lat"]] for row in observations],
        {"kind": "raw_gps_sequence", "sequence_id": sequence_id, "segment_index": segment_index},
    )
    if not graphhopper_result.get("available"):
        return {
            "available": False,
            "reason": graphhopper_result.get("reason", "GraphHopper map matching unavailable"),
            "layers": {
                "points": {"type": "FeatureCollection", "features": raw_points},
                "links": {"type": "FeatureCollection", "features": []},
                "matched_roads": {"type": "FeatureCollection", "features": []},
                "raw_trajectory": {"type": "FeatureCollection", "features": [raw_trajectory] if raw_trajectory else []},
                "matched_trajectory": {"type": "FeatureCollection", "features": []},
            },
            "meta": {
                "count": len(observations),
                "matched": 0,
                "sequence_id": sequence_id,
                "profile": profile,
                "method": "graphhopper_sequence_map_matching",
            },
        }

    matched_geometry = graphhopper_result["geometry"]
    snapped_waypoints = _snapped_waypoint_coordinates(graphhopper_result)
    blue_line_waypoints = _nearest_points_on_linestring(observations, matched_geometry)
    if not snapped_waypoints:
        snapped_waypoints = blue_line_waypoints
    point_features = []
    link_features = []
    road_features_by_id: dict[str, dict] = {}
    local_override_count = 0
    for index, observation in enumerate(observations):
        graphhopper_snapped = snapped_waypoints[index] if index < len(snapped_waypoints) else None
        blue_line_snapped = blue_line_waypoints[index] if index < len(blue_line_waypoints) else graphhopper_snapped
        mapmatched_geometry = (
            {"type": "Point", "coordinates": blue_line_snapped}
            if blue_line_snapped
            else None
        )
        graphhopper_distance = (
            _haversine_m(
                float(observation["lon"]),
                float(observation["lat"]),
                float(graphhopper_snapped[0]),
                float(graphhopper_snapped[1]),
            )
            if graphhopper_snapped
            else None
        )
        snapped = blue_line_snapped
        snapped_distance = (
            _haversine_m(
                float(observation["lon"]),
                float(observation["lat"]),
                float(snapped[0]),
                float(snapped[1]),
            )
            if snapped
            else None
        )
        snap_properties = {
            "snap_source": "graphhopper_matched_trajectory_projection",
            "distance_m": round(snapped_distance, 2) if snapped_distance is not None else None,
            "local_osm_id": None,
            "local_highway": None,
            "local_surface": None,
            "local_road_category": None,
            "local_distance_m": None,
        }
        snap_properties["graphhopper_distance_m"] = (
            round(graphhopper_distance, 2) if graphhopper_distance is not None else None
        )
        snap_properties["gps_geometry"] = observation.get("gps_geometry") or {
            "type": "Point",
            "coordinates": [observation["lon"], observation["lat"]],
        }
        snap_properties["mapillary_geometry"] = observation.get("mapillary_geometry")
        snap_properties["mapmatched_geometry"] = mapmatched_geometry
        snap_properties["mapmatched_source"] = "graphhopper_matched_trajectory_projection"
        snap_properties["profile"] = profile
        point_features.append(
            _map_matching_point_feature(
                observation,
                snapped,
                segment_index=segment_index,
                extra_properties=snap_properties,
            )
        )
        if snapped:
            link_features.append(
                {
                    "type": "Feature",
                    "id": f"map-match-link/{segment_index}/{observation['image_id']}",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[observation["lon"], observation["lat"]], snapped],
                    },
                    "properties": {
                        "image_id": observation["image_id"],
                        "idx": observation["idx"],
                        "sequence_id": sequence_id,
                        "segment_index": segment_index,
                        **snap_properties,
                    },
                }
            )

    return {
        "available": True,
        "layers": {
            "points": {"type": "FeatureCollection", "features": point_features},
            "links": {"type": "FeatureCollection", "features": link_features},
            "matched_roads": {"type": "FeatureCollection", "features": list(road_features_by_id.values())},
            "raw_trajectory": {"type": "FeatureCollection", "features": [raw_trajectory] if raw_trajectory else []},
            "matched_trajectory": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": f"graphhopper-matched-trajectory/{segment_index}",
                        "geometry": matched_geometry,
                        "properties": {
                            "kind": "graphhopper_matched",
                            "sequence_id": sequence_id,
                            "segment_index": segment_index,
                            "profile": profile,
                            "distance": graphhopper_result.get("distance"),
                            "time": graphhopper_result.get("time"),
                            "local_override_count": local_override_count,
                            "profile_score": graphhopper_result.get("profile_score"),
                            "profile_scores": graphhopper_result.get("profile_scores"),
                        },
                    }
                ],
            },
        },
        "meta": {
            "count": len(observations),
            "matched": len(point_features),
            "sequence_id": sequence_id,
            "profile": profile,
            "distance": graphhopper_result.get("distance"),
            "time": graphhopper_result.get("time"),
            "local_override_count": local_override_count,
            "profile_score": graphhopper_result.get("profile_score"),
            "profile_scores": graphhopper_result.get("profile_scores"),
            "method": "graphhopper_sequence_map_matching",
        },
    }


def _map_matching_point_feature(
    observation: dict,
    snapped: list[float] | None,
    *,
    segment_index: int = 0,
    extra_properties: dict | None = None,
) -> dict:
    properties = {
        key: observation.get(key)
        for key in (
            "image_id",
            "idx",
            "captured_at",
            "sequence_id",
            "heading_deg",
            "track_heading_deg",
            "capture_position",
            "surface_material",
            "thumb_256_url",
            "thumb_1024_url",
            "mapillary_url",
            "camera_type",
            "width",
            "height",
            "gps_geometry",
            "mapillary_geometry",
        )
    }
    properties.update(
        {
            "road_category": None,
            "highway": "graphhopper_match",
            "surface": None,
            "distance_m": None,
            "score": None,
            "segment_index": segment_index,
            "snapped_geometry": {"type": "Point", "coordinates": snapped} if snapped else None,
        }
    )
    if extra_properties:
        properties.update(extra_properties)
    return {
        "type": "Feature",
        "id": f"map-match-point/{segment_index}/{observation['image_id']}",
        "geometry": {"type": "Point", "coordinates": [observation["lon"], observation["lat"]]},
        "properties": properties,
    }


def _line_feature(feature_id: str, coordinates: list[list[float]], properties: dict) -> dict | None:
    if len(coordinates) < 2:
        return None
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": {"type": "LineString", "coordinates": coordinates},
        "properties": properties,
    }


def _snapped_waypoint_coordinates(graphhopper_result: dict) -> list[list[float]]:
    geometry = graphhopper_result.get("snapped_waypoints")
    if not isinstance(geometry, dict):
        return []
    if geometry.get("type") == "Point":
        coordinates = geometry.get("coordinates")
        return [coordinates] if isinstance(coordinates, list) else []
    if geometry.get("type") == "LineString":
        coordinates = geometry.get("coordinates")
        return coordinates if isinstance(coordinates, list) else []
    if geometry.get("type") == "MultiPoint":
        coordinates = geometry.get("coordinates")
        return coordinates if isinstance(coordinates, list) else []
    return []


def _nearest_points_on_linestring(observations: list[dict], geometry: dict) -> list[list[float]]:
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        return []
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return []
    return [
        _nearest_point_on_polyline(float(row["lon"]), float(row["lat"]), coordinates)
        for row in observations
    ]


def _nearest_point_on_polyline(lon: float, lat: float, coordinates: list) -> list[float]:
    meters_per_degree_lat = 111_320.0
    meters_per_degree_lon = meters_per_degree_lat * max(math.cos(math.radians(lat)), 1e-9)
    best_point: list[float] | None = None
    best_distance_sq = float("inf")
    for start, end in zip(coordinates, coordinates[1:]):
        if not isinstance(start, list) or not isinstance(end, list) or len(start) < 2 or len(end) < 2:
            continue
        ax = (float(start[0]) - lon) * meters_per_degree_lon
        ay = (float(start[1]) - lat) * meters_per_degree_lat
        bx = (float(end[0]) - lon) * meters_per_degree_lon
        by = (float(end[1]) - lat) * meters_per_degree_lat
        dx = bx - ax
        dy = by - ay
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            t = 0.0
        else:
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
        px = ax + t * dx
        py = ay + t * dy
        distance_sq = px * px + py * py
        if distance_sq < best_distance_sq:
            best_distance_sq = distance_sq
            best_point = [
                lon + px / meters_per_degree_lon,
                lat + py / meters_per_degree_lat,
            ]
    return best_point or [lon, lat]


def _compatible_track_neighbor(row: dict, neighbor: dict) -> dict | None:
    row_sequence = row.get("sequence_id")
    neighbor_sequence = neighbor.get("sequence_id")
    if row_sequence and neighbor_sequence:
        return neighbor if row_sequence == neighbor_sequence else None
    time_gap_ms = abs(float(row.get("captured_at") or 0) - float(neighbor.get("captured_at") or 0))
    if time_gap_ms > 120_000:
        return None
    if _haversine_m(row["lon"], row["lat"], neighbor["lon"], neighbor["lat"]) > 50:
        return None
    return neighbor


def _optional_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    return 2.0 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _bearing_degrees(lon1: float, lat1: float, lon2: float, lat2: float) -> float | None:
    if lon1 == lon2 and lat1 == lat2:
        return None
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)
    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _cell_center_geometry(grid_id: str) -> dict | None:
    try:
        ring = cell_from_id(grid_id).ring_wgs84
    except ValueError:
        return None
    west = min(coordinate[0] for coordinate in ring)
    east = max(coordinate[0] for coordinate in ring)
    south = min(coordinate[1] for coordinate in ring)
    north = max(coordinate[1] for coordinate in ring)
    return {
        "type": "Point",
        "coordinates": [(west + east) / 2, (south + north) / 2],
    }


@app.get("/api/grids/by-point")
async def grid_by_point(
    longitude: float = Query(ge=-180, le=180),
    latitude: float = Query(ge=-90, le=90),
) -> dict:
    try:
        cell = cell_for_point(longitude, latitude)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return cell.to_feature(selected=True)


@app.get("/api/grids/around")
async def grids_around(
    longitude: float = Query(ge=-180, le=180),
    latitude: float = Query(ge=-90, le=90),
    radius: int = Query(default=3, ge=0, le=10),
) -> dict:
    center = cell_for_point(longitude, latitude)
    return {
        "type": "FeatureCollection",
        "features": [
            cell.to_feature(selected=cell == center)
            for cell in neighboring_cells(center, radius)
        ],
        "meta": {"center_grid_id": center.grid_id, "radius": radius},
    }


@app.get("/api/grids/{grid_id}/around")
async def grids_around_id(
    grid_id: str,
    radius: int = Query(default=1, ge=0, le=10),
) -> dict:
    try:
        center = cell_from_id(grid_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "type": "FeatureCollection",
        "features": [
            cell.to_feature(selected=cell == center)
            for cell in neighboring_cells(center, radius)
        ],
        "meta": {"center_grid_id": center.grid_id, "radius": radius},
    }


@app.get("/api/grids/{grid_id}/images")
async def images_for_grid(
    grid_id: str,
    request: Request,
    refresh: bool = Query(default=False),
) -> JSONResponse:
    try:
        cell = cell_from_id(grid_id)
        result = await request.app.state.mapillary.images_for_cell(cell, refresh)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MapillaryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MapillaryAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    headers = {
        "Content-Disposition": f'inline; filename="{grid_id}-mapillary.geojson"',
        "X-Mapillary-Cache": result["meta"]["cache"],
    }
    return JSONResponse(result, headers=headers)


@app.get("/api/grids/{grid_id}/images.geojson")
async def download_grid_images(grid_id: str, request: Request) -> JSONResponse:
    response = await images_for_grid(grid_id, request, refresh=False)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{grid_id}-mapillary.geojson"'
    )
    return response
