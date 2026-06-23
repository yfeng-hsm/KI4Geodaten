from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Iterable

from pyproj import Transformer


GRID_SIZE_METERS = 100
GRID_CRS = "EPSG:3035"
WGS84_CRS = "EPSG:4326"
GRID_ID_PATTERN = re.compile(
    r"^CRS3035RES100mN(?P<north>\d+)E(?P<east>\d+)$"
)

TO_GRID = Transformer.from_crs(WGS84_CRS, GRID_CRS, always_xy=True)
TO_WGS84 = Transformer.from_crs(GRID_CRS, WGS84_CRS, always_xy=True)


@dataclass(frozen=True)
class CensusGridCell:
    east: int
    north: int

    @property
    def grid_id(self) -> str:
        return f"CRS3035RES100mN{self.north}E{self.east}"

    @property
    def bounds_3035(self) -> tuple[int, int, int, int]:
        return (
            self.east,
            self.north,
            self.east + GRID_SIZE_METERS,
            self.north + GRID_SIZE_METERS,
        )

    @property
    def ring_wgs84(self) -> list[list[float]]:
        west, south, east, north = self.bounds_3035
        corners = [
            (west, south),
            (east, south),
            (east, north),
            (west, north),
            (west, south),
        ]
        return [[lon, lat] for lon, lat in (TO_WGS84.transform(*p) for p in corners)]

    @property
    def bbox_wgs84(self) -> tuple[float, float, float, float]:
        ring = self.ring_wgs84
        longitudes = [point[0] for point in ring]
        latitudes = [point[1] for point in ring]
        return min(longitudes), min(latitudes), max(longitudes), max(latitudes)

    def contains_wgs84(self, longitude: float, latitude: float) -> bool:
        east, north = TO_GRID.transform(longitude, latitude)
        west, south, max_east, max_north = self.bounds_3035
        return west <= east < max_east and south <= north < max_north

    def to_feature(self, selected: bool = False) -> dict:
        west, south, east, north = self.bounds_3035
        return {
            "type": "Feature",
            "id": self.grid_id,
            "geometry": {"type": "Polygon", "coordinates": [self.ring_wgs84]},
            "properties": {
                "grid_id": self.grid_id,
                "resolution_m": GRID_SIZE_METERS,
                "crs": GRID_CRS,
                "x_sw": west,
                "y_sw": south,
                "x_center": west + GRID_SIZE_METERS // 2,
                "y_center": south + GRID_SIZE_METERS // 2,
                "selected": selected,
            },
        }


def cell_for_point(longitude: float, latitude: float) -> CensusGridCell:
    east, north = TO_GRID.transform(longitude, latitude)
    if not math.isfinite(east) or not math.isfinite(north):
        raise ValueError("The coordinate cannot be transformed to EPSG:3035")
    return CensusGridCell(
        east=math.floor(east / GRID_SIZE_METERS) * GRID_SIZE_METERS,
        north=math.floor(north / GRID_SIZE_METERS) * GRID_SIZE_METERS,
    )


def cell_from_id(grid_id: str) -> CensusGridCell:
    match = GRID_ID_PATTERN.fullmatch(grid_id)
    if not match:
        raise ValueError("Invalid 100m INSPIRE grid ID")
    return CensusGridCell(
        east=int(match.group("east")),
        north=int(match.group("north")),
    )


def neighboring_cells(cell: CensusGridCell, radius: int) -> Iterable[CensusGridCell]:
    if not 0 <= radius <= 10:
        raise ValueError("Grid radius must be between 0 and 10")
    for north_offset in range(-radius, radius + 1):
        for east_offset in range(-radius, radius + 1):
            yield CensusGridCell(
                east=cell.east + east_offset * GRID_SIZE_METERS,
                north=cell.north + north_offset * GRID_SIZE_METERS,
            )
