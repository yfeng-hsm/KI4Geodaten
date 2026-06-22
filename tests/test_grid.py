import pytest

from app.grid import (
    GRID_SIZE_METERS,
    cell_for_point,
    cell_from_id,
    neighboring_cells,
)


def test_berlin_point_maps_to_an_inspire_100m_cell():
    cell = cell_for_point(13.4095, 52.5208)

    assert cell.grid_id == "CRS3035RES100mN3273300E4552300"
    assert "E" in cell.grid_id
    assert cell.east % GRID_SIZE_METERS == 0
    assert cell.north % GRID_SIZE_METERS == 0
    assert cell.contains_wgs84(13.4095, 52.5208)


def test_grid_id_round_trip():
    original = cell_for_point(13.4095, 52.5208)
    restored = cell_from_id(original.grid_id)

    assert restored == original
    assert restored.bounds_3035[2] - restored.bounds_3035[0] == 100
    assert restored.bounds_3035[3] - restored.bounds_3035[1] == 100


def test_grid_polygon_is_closed():
    ring = cell_for_point(13.4095, 52.5208).ring_wgs84

    assert len(ring) == 5
    assert ring[0] == ring[-1]


def test_neighboring_cells_include_center():
    center = cell_for_point(13.4095, 52.5208)
    cells = list(neighboring_cells(center, radius=2))

    assert len(cells) == 25
    assert center in cells


def test_invalid_grid_id_is_rejected():
    with pytest.raises(ValueError, match="Invalid"):
        cell_from_id("not-a-grid")
