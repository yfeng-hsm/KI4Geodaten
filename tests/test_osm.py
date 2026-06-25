from app.osm import (
    OSMStore,
    grouped_surface_counts,
    majority_surface,
    matched_road_surface_material,
    normalize_osm_surface,
    normalize_vlm_surface,
    offset_point_geometry,
    relative_side,
)


def test_osm_store_without_database_is_unavailable():
    store = OSMStore(None)

    assert store.status()["available"] is False
    result = store.cell_layers(
        "CRS3035RES100mN2987100E4196900",
        {"type": "Polygon", "coordinates": [[]]},
    )

    assert result["available"] is False
    assert result["layers"]["roads"]["features"] == []
    assert store.road_vlm_matches(1) is None
    assert store.cell_road_surface_validation(
        "CRS3035RES100mN2987100E4196900",
        {"type": "Polygon", "coordinates": [[]]},
    )["available"] is False


def test_surface_normalization_groups_semantically_equivalent_values():
    assert normalize_osm_surface("asphalt") == "asphalt"
    assert normalize_osm_surface("concrete:plates") == "concrete"
    assert normalize_osm_surface("sett") == "sett"
    assert normalize_vlm_surface("sett") == "sett"
    assert normalize_osm_surface("compacted") == "unpaved"
    assert normalize_osm_surface("grass_paver") == "unpaved"
    assert normalize_osm_surface("fine_gravel") == "unpaved"
    assert normalize_vlm_surface("uncertain") is None


def test_grouped_surface_counts_and_majority_ignore_uncertain_values():
    counts = grouped_surface_counts({
        "unpaved": 2,
        "uncertain": 8,
        "asphalt": 1,
    })

    assert counts == {"unpaved": 2, "asphalt": 1}
    assert majority_surface(counts) == "unpaved"


def test_relative_side_uses_camera_heading():
    assert relative_side(0, 90) == "right"
    assert relative_side(0, 270) == "left"
    assert relative_side(90, 90) == "front"


def test_matched_road_surface_uses_adjacent_pedestrian_observation():
    fields = {
        "capture_position": "pedestrian_road",
        "surface_material": "paving_stones",
        "left_adjacent_road_type": "vehicle_road",
        "left_adjacent_road_surface_material": "asphalt",
        "right_adjacent_road_type": "bicycle_road",
        "right_adjacent_road_surface_material": "sett",
    }

    assert matched_road_surface_material(fields, "pedestrian", "vehicle", "left") == (
        "asphalt",
        "left_adjacent_road",
    )
    assert matched_road_surface_material(fields, "pedestrian", "bicycle", "right") == (
        "sett",
        "right_adjacent_road",
    )
    assert matched_road_surface_material(fields, "pedestrian", "vehicle", "right") == (
        None,
        "none",
    )


def test_offset_point_geometry_moves_left_and_right_from_heading():
    point = {"type": "Point", "coordinates": [8.0, 50.0]}

    left = offset_point_geometry(point, 0, "left", meters=5)
    right = offset_point_geometry(point, 0, "right", meters=5)

    assert left is not None
    assert right is not None
    assert left["coordinates"][0] < point["coordinates"][0]
    assert right["coordinates"][0] > point["coordinates"][0]
