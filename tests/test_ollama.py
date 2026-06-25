from app.ollama import _image_url_for_analysis, _normalize_result


def test_normalize_result_accepts_allowed_values():
    result = _normalize_result(
        {
            "capture_position": "vehicle_road",
            "surface_material": "asphalt",
            "confidence": 1.5,
            "reason": "visible asphalt road",
        }
    )

    assert result["capture_position"] == "vehicle_road"
    assert result["surface_material"] == "asphalt"
    assert result["unusable_reason"] == "none"
    assert result["confidence"] == 1.0
    assert result["left_sidewalk"] == "uncertain"
    assert result["left_sidewalk_surface_material"] is None
    assert result["left_adjacent_road_type"] is None
    assert result["left_adjacent_road_surface_material"] is None


def test_normalize_result_forces_transit_images_to_other_location():
    result = _normalize_result(
        {
            "unusable_reason": "transit_vehicle",
            "capture_position": "vehicle_road",
            "surface_material": "asphalt",
            "left_sidewalk": "yes",
            "left_sidewalk_surface_material": "concrete",
        }
    )

    assert result["unusable_reason"] == "transit_vehicle"
    assert result["capture_position"] == "other_location"
    assert result["surface_material"] == "uncertain"
    assert result["left_sidewalk"] is None
    assert result["left_sidewalk_surface_material"] is None


def test_normalize_result_forces_poor_quality_images_to_unusable():
    result = _normalize_result(
        {
            "unusable_reason": "poor_image_quality",
            "capture_position": "pedestrian_road",
            "surface_material": "paving_stones",
            "left_adjacent_road_type": "vehicle_road",
            "left_adjacent_road_surface_material": "asphalt",
        }
    )

    assert result["unusable_reason"] == "poor_image_quality"
    assert result["capture_position"] == "other_location"
    assert result["surface_material"] == "uncertain"
    assert result["left_adjacent_road_type"] is None
    assert result["left_adjacent_road_surface_material"] is None


def test_normalize_result_rejects_unexpected_values():
    result = _normalize_result(
        {
            "capture_position": "street",
            "surface_material": "brick",
            "confidence": "bad",
        }
    )

    assert result["capture_position"] == "uncertain"
    assert result["surface_material"] == "uncertain"
    assert result["confidence"] == 0.0
    assert result["left_sidewalk"] is None
    assert result["right_sidewalk_surface_material"] is None


def test_normalize_result_keeps_vehicle_sidewalk_fields():
    result = _normalize_result(
        {
            "capture_position": "vehicle_road",
            "surface_material": "asphalt",
            "left_sidewalk": "yes",
            "left_sidewalk_surface_material": "paving_stones",
            "right_sidewalk": "no",
            "right_sidewalk_surface_material": "concrete",
        }
    )

    assert result["left_sidewalk"] == "yes"
    assert result["left_sidewalk_surface_material"] == "paving_stones"
    assert result["right_sidewalk"] == "no"
    assert result["right_sidewalk_surface_material"] is None


def test_normalize_result_accepts_sett_surface_materials():
    result = _normalize_result(
        {
            "capture_position": "vehicle_road",
            "surface_material": "sett",
            "left_sidewalk": "yes",
            "left_sidewalk_surface_material": "sett",
            "right_sidewalk": "yes",
            "right_sidewalk_surface_material": "paving_stones",
        }
    )

    assert result["surface_material"] == "sett"
    assert result["left_sidewalk_surface_material"] == "sett"
    assert result["right_sidewalk_surface_material"] == "paving_stones"


def test_normalize_result_nulls_sidewalk_fields_for_non_vehicle_positions():
    result = _normalize_result(
        {
            "capture_position": "pedestrian_road",
            "surface_material": "paving_stones",
            "left_sidewalk": "yes",
            "left_sidewalk_surface_material": "concrete",
            "right_sidewalk": "yes",
            "right_sidewalk_surface_material": "asphalt",
        }
    )

    assert result["left_sidewalk"] is None
    assert result["left_sidewalk_surface_material"] is None
    assert result["right_sidewalk"] is None
    assert result["right_sidewalk_surface_material"] is None


def test_normalize_result_keeps_pedestrian_adjacent_road_fields():
    result = _normalize_result(
        {
            "capture_position": "pedestrian_road",
            "surface_material": "paving_stones",
            "left_adjacent_road_type": "vehicle_road",
            "left_adjacent_road_surface_material": "asphalt",
            "right_adjacent_road_type": "bicycle_road",
            "right_adjacent_road_surface_material": "sett",
        }
    )

    assert result["left_adjacent_road_type"] == "vehicle_road"
    assert result["left_adjacent_road_surface_material"] == "asphalt"
    assert result["right_adjacent_road_type"] == "bicycle_road"
    assert result["right_adjacent_road_surface_material"] == "sett"


def test_normalize_result_nulls_adjacent_road_fields_for_non_pedestrian_positions():
    result = _normalize_result(
        {
            "capture_position": "vehicle_road",
            "surface_material": "asphalt",
            "left_adjacent_road_type": "bicycle_road",
            "left_adjacent_road_surface_material": "sett",
        }
    )

    assert result["left_adjacent_road_type"] is None
    assert result["left_adjacent_road_surface_material"] is None
    assert result["right_adjacent_road_type"] is None
    assert result["right_adjacent_road_surface_material"] is None


def test_image_url_for_analysis_prefers_configured_small_thumbnail():
    properties = {
        "thumb_256_url": "https://example.test/256.jpg",
        "thumb_1024_url": "https://example.test/1024.jpg",
    }

    assert _image_url_for_analysis(properties, 256).endswith("/256.jpg")


def test_image_url_for_analysis_falls_back_to_1024():
    properties = {"thumb_1024_url": "https://example.test/1024.jpg"}

    assert _image_url_for_analysis(properties, 256).endswith("/1024.jpg")


def test_image_url_for_analysis_uses_1024_for_local_512_resize():
    properties = {
        "thumb_256_url": "https://example.test/256.jpg",
        "thumb_1024_url": "https://example.test/1024.jpg",
    }

    assert _image_url_for_analysis(properties, 512).endswith("/1024.jpg")
