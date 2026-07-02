from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings


class ValhallaClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def status(self) -> dict[str, Any]:
        if not self.settings.valhalla_base_url:
            return {"configured": False, "ok": False, "error": "VALHALLA_BASE_URL is not configured"}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.settings.valhalla_base_url}/status")
            return {
                "configured": True,
                "ok": response.status_code < 500,
                "status_code": response.status_code,
            }
        except httpx.HTTPError as exc:
            return {"configured": True, "ok": False, "error": str(exc)}

    async def match_trace(
        self,
        observations: list[dict[str, Any]],
        *,
        profile: str,
        gps_accuracy: float = 15,
    ) -> dict[str, Any]:
        if not self.settings.valhalla_base_url:
            return self._unavailable("VALHALLA_BASE_URL is not configured")
        if len(observations) < 2:
            return self._unavailable("At least two observations are required")

        costing = _valhalla_costing(profile)
        payload = {
            "shape": [_observation_to_shape_point(observation) for observation in observations],
            "costing": costing,
            "shape_match": "map_snap",
            "trace_options": {
                "gps_accuracy": max(1, int(round(gps_accuracy))),
                "search_radius": max(30, int(round(gps_accuracy * 6))),
                "breakage_distance": 2000,
                "interpolation_distance": 10,
            },
            "directions_options": {"units": "kilometers"},
            "format": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.valhalla_timeout_seconds) as client:
                response = await client.post(f"{self.settings.valhalla_base_url}/trace_route", json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return self._unavailable(
                f"Valhalla /trace_route returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            )
        except httpx.HTTPError as exc:
            return self._unavailable(f"Valhalla /trace_route request failed: {exc}")

        try:
            body = response.json()
        except ValueError:
            return self._unavailable("Valhalla /trace_route did not return JSON")

        trip = body.get("trip") or {}
        legs = trip.get("legs") or []
        coordinates: list[list[float]] = []
        for leg in legs:
            shape = leg.get("shape")
            if isinstance(shape, str) and shape:
                decoded = decode_valhalla_polyline(shape)
                if coordinates and decoded and coordinates[-1] == decoded[0]:
                    coordinates.extend(decoded[1:])
                else:
                    coordinates.extend(decoded)
        if len(coordinates) < 2:
            message = trip.get("status_message") or body.get("error") or "Valhalla returned no matched LineString"
            return self._unavailable(str(message))

        summary = trip.get("summary") or {}
        return {
            "available": True,
            "provider": "valhalla",
            "profile": profile,
            "geometry": {"type": "LineString", "coordinates": coordinates},
            "snapped_waypoints": None,
            "distance": _distance_m(summary),
            "time": _time_ms(summary),
            "raw_response": {
                key: value
                for key, value in body.items()
                if key in {"id", "status", "status_message", "error"}
            },
        }

    @staticmethod
    def _unavailable(reason: str) -> dict[str, Any]:
        return {"available": False, "reason": reason}


def _valhalla_costing(profile: str) -> str:
    return {
        "car": "auto",
        "bike": "bicycle",
        "foot": "pedestrian",
    }.get(profile, "pedestrian")


def _observation_to_shape_point(observation: dict[str, Any]) -> dict[str, Any]:
    point = {
        "lat": float(observation["lat"]),
        "lon": float(observation["lon"]),
    }
    timestamp = _epoch_seconds(observation.get("captured_at"))
    if timestamp is not None:
        point["time"] = timestamp
    return point


def _epoch_seconds(captured_at: Any) -> int | None:
    try:
        value = float(captured_at)
    except (TypeError, ValueError):
        return None
    if value > 10_000_000_000:
        value = value / 1000.0
    try:
        return int(datetime.fromtimestamp(value, timezone.utc).timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _distance_m(summary: dict[str, Any]) -> float | None:
    try:
        return float(summary["length"]) * 1000.0
    except (KeyError, TypeError, ValueError):
        return None


def _time_ms(summary: dict[str, Any]) -> int | None:
    try:
        return int(float(summary["time"]) * 1000)
    except (KeyError, TypeError, ValueError):
        return None


def decode_valhalla_polyline(polyline: str, precision: int = 6) -> list[list[float]]:
    coordinates: list[list[float]] = []
    index = 0
    lat = 0
    lon = 0
    factor = 10 ** precision
    while index < len(polyline):
        delta_lat, index = _decode_polyline_value(polyline, index)
        delta_lon, index = _decode_polyline_value(polyline, index)
        lat += delta_lat
        lon += delta_lon
        coordinates.append([lon / factor, lat / factor])
    return coordinates


def _decode_polyline_value(polyline: str, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = ord(polyline[index]) - 63
        index += 1
        result |= (byte & 0x1F) << shift
        shift += 5
        if byte < 0x20:
            break
    value = ~(result >> 1) if result & 1 else result >> 1
    return value, index
