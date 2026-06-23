from app.vlm_store import preferred_mapillary_geometry


def test_preferred_mapillary_geometry_uses_computed_before_original():
    current = {"type": "Point", "coordinates": [1, 1]}
    original = {"type": "Point", "coordinates": [2, 2]}
    computed = {"type": "Point", "coordinates": [3, 3]}

    assert preferred_mapillary_geometry(
        current,
        {"original_geometry": original, "computed_geometry": computed},
    ) == computed


def test_preferred_mapillary_geometry_falls_back_to_original_then_current():
    current = {"type": "Point", "coordinates": [1, 1]}
    original = {"type": "Point", "coordinates": [2, 2]}

    assert preferred_mapillary_geometry(current, {"original_geometry": original}) == original
    assert preferred_mapillary_geometry(current, {}) == current
