from app.osm import OSMStore


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
