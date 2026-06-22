from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.census import CensusStore
from app.config import Settings
from app.grid import cell_for_point, cell_from_id, neighboring_cells
from app.mapillary import (
    MapillaryAPIError,
    MapillaryClient,
    MapillaryConfigurationError,
)


STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    app.state.settings = settings
    app.state.mapillary = MapillaryClient(settings)
    app.state.census = CensusStore(Path(__file__).parent / "data" / "census")
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
        "grid_crs": "EPSG:3035",
        "grid_resolution_m": 100,
        "census_city": request.app.state.census.meta["city"],
        "census_cell_count": request.app.state.census.meta["cell_count"],
    }


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
