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
    assert result["confidence"] == 1.0


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


def test_image_url_for_analysis_prefers_configured_small_thumbnail():
    properties = {
        "thumb_256_url": "https://example.test/256.jpg",
        "thumb_1024_url": "https://example.test/1024.jpg",
    }

    assert _image_url_for_analysis(properties, 256).endswith("/256.jpg")


def test_image_url_for_analysis_falls_back_to_1024():
    properties = {"thumb_1024_url": "https://example.test/1024.jpg"}

    assert _image_url_for_analysis(properties, 256).endswith("/1024.jpg")
