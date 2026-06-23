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
    app.state.vlm_jobs = {}
    yield


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
    return request.app.state.vlm_store.results_for_grid(grid_id)


@app.post("/api/grids/{grid_id}/vlm-jobs")
async def start_vlm_job(grid_id: str, payload: dict, request: Request) -> dict:
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        raise HTTPException(status_code=422, detail="images list is required")

    model = str(payload.get("model") or request.app.state.settings.ollama_model)
    force = bool(payload.get("force") or payload.get("overwrite_existing"))
    job_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    request.app.state.vlm_jobs[job_id] = {
        "job_id": job_id,
        "grid_id": grid_id,
        "model": model,
        "force": force,
        "status": "queued",
        "total": len(images),
        "processed": 0,
        "analyzed": 0,
        "skipped": 0,
        "failed": 0,
        "current_image_id": None,
        "last_stored_image_id": None,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "error": None,
    }
    asyncio.create_task(_run_vlm_job(request.app, job_id, grid_id, images, model, force))
    return request.app.state.vlm_jobs[job_id]


@app.get("/api/vlm/jobs/{job_id}")
async def vlm_job_status(job_id: str, request: Request) -> dict:
    job = request.app.state.vlm_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="VLM job not found")
    return job


async def _run_vlm_job(
    app: FastAPI, job_id: str, grid_id: str, images: list, model: str, force: bool
) -> None:
    job = app.state.vlm_jobs[job_id]
    job["status"] = "running"
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        image_ids = [_image_id(image) for image in images]
        existing_ids = set() if force else app.state.vlm_store.existing_image_ids(image_ids)
        async with httpx.AsyncClient(timeout=app.state.settings.ollama_timeout_seconds) as client:
            for image in images:
                image_id = _image_id(image)
                job["current_image_id"] = image_id
                if image_id in existing_ids:
                    job["skipped"] += 1
                    job["processed"] += 1
                    job["updated_at"] = datetime.now(timezone.utc).isoformat()
                    continue
                result = await app.state.ollama.analyze_one(client, image, model)
                if not result.get("ok"):
                    job["failed"] += 1
                stored = app.state.vlm_store.upsert(grid_id, result)
                job["last_stored_image_id"] = stored["image_id"]
                job["analyzed"] += 1
                job["processed"] += 1
                job["updated_at"] = datetime.now(timezone.utc).isoformat()
        job["status"] = "completed"
        job["completed_at"] = datetime.now(timezone.utc).isoformat()
        job["current_image_id"] = None
    except Exception as exc:  # pragma: no cover - defensive job boundary
        job["status"] = "failed"
        job["error"] = f"{exc.__class__.__name__}: {exc}"
        job["updated_at"] = datetime.now(timezone.utc).isoformat()


def _image_id(image: dict) -> str:
    return str(image.get("id") or image.get("properties", {}).get("id") or "")


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
