import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

from app.census import CensusStore
from app.config import Settings
from app.grid import cell_for_point, cell_from_id, neighboring_cells
from app.mapillary import (
    MapillaryAPIError,
    MapillaryClient,
    MapillaryConfigurationError,
)
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
    app.state.census = CensusStore(Path(__file__).parent / "data" / "census")
    app.state.osm = OSMStore(settings.database_url)
    app.state.ollama = OllamaClient(settings)
    app.state.vlm_store = VLMAnalysisStore(settings.database_url)
    app.state.vlm_store.ensure_schema()
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
async def mainz_grid_map_layers(grid_id: str, request: Request) -> dict:
    census: CensusStore = request.app.state.census
    census_cell = census.cell(grid_id)
    if census_cell is None:
        raise HTTPException(status_code=404, detail="Grid cell is not in Mainz")
    try:
        cell = cell_from_id(grid_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return request.app.state.osm.cell_layers(
        grid_id=grid_id,
        cell_geometry=cell.to_feature()["geometry"],
    )


@app.get("/api/osm/roads/{osm_id}")
async def osm_road(osm_id: int, request: Request) -> dict:
    road = request.app.state.osm.road(osm_id)
    if road is None:
        raise HTTPException(status_code=404, detail="OSM road is not available")
    return road


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
        stored = request.app.state.vlm_store.upsert(grid_id, result["results"][0])
        result["stored"] = stored
    return result


@app.get("/api/grids/{grid_id}/vlm-results")
async def vlm_results_for_grid(grid_id: str, request: Request) -> dict:
    result = request.app.state.vlm_store.results_for_grid(grid_id)
    return _enrich_vlm_results_from_cache(result, request.app.state.mapillary.cached_image_index())


@app.get("/api/vlm-results")
async def all_vlm_results(
    request: Request,
    limit: int = Query(default=5000, ge=1, le=50000),
) -> dict:
    result = request.app.state.vlm_store.all_results(limit=limit)
    return _enrich_vlm_results_from_cache(result, request.app.state.mapillary.cached_image_index())


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
    try:
        image_ids = [_image_id(image) for image in images]
        existing_ids = set() if force else app.state.vlm_store.existing_image_ids(image_ids)
        async with httpx.AsyncClient(timeout=app.state.settings.ollama_timeout_seconds) as client:
            for image in images:
                image_id = _image_id(image)
                app.state.vlm_store.update_job(
                    job_id,
                    current_image_id=image_id,
                    processed=processed,
                    analyzed=analyzed,
                    skipped=skipped,
                    failed=failed,
                )
                app.state.vlm_store.update_job_item(job_id, image_id, "running")
                if image_id in existing_ids:
                    skipped += 1
                    processed += 1
                    app.state.vlm_store.update_job_item(job_id, image_id, "skipped")
                    app.state.vlm_store.update_job(
                        job_id,
                        processed=processed,
                        skipped=skipped,
                        current_image_id=None,
                    )
                    continue
                result = await app.state.ollama.analyze_one(client, image, model)
                if not result.get("ok"):
                    failed += 1
                stored = app.state.vlm_store.upsert(grid_id, result)
                analyzed += 1
                processed += 1
                app.state.vlm_store.update_job_item(
                    job_id,
                    image_id,
                    "failed" if result.get("error") else "completed",
                    result.get("error"),
                )
                app.state.vlm_store.update_job(
                    job_id,
                    processed=processed,
                    analyzed=analyzed,
                    failed=failed,
                    current_image_id=None,
                    last_stored_image_id=stored["image_id"],
                )
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
