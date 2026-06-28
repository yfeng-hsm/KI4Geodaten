from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any

import httpx

from app.config import Settings


class GraphHopperClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def status(self) -> dict[str, Any]:
        if not self.settings.graphhopper_base_url:
            return {"configured": False, "ok": False, "error": "GRAPHHOPPER_BASE_URL is not configured"}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.settings.graphhopper_base_url}/health")
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
        if not self.settings.graphhopper_base_url:
            return self._unavailable("GRAPHHOPPER_BASE_URL is not configured")
        if len(observations) < 2:
            return self._unavailable("At least two observations are required")

        gpx = _observations_to_gpx(observations)
        params = {
            "profile": profile,
            "type": "json",
            "points_encoded": "false",
            "gps_accuracy": str(gps_accuracy),
            "instructions": "false",
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.graphhopper_timeout_seconds) as client:
                response = await client.post(
                    f"{self.settings.graphhopper_base_url}/match",
                    params=params,
                    content=gpx.encode("utf-8"),
                    headers={"Content-Type": "application/gpx+xml"},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return self._unavailable(
                f"GraphHopper /match returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            )
        except httpx.HTTPError as exc:
            return self._unavailable(f"GraphHopper /match request failed: {exc}")

        try:
            body = response.json()
        except ValueError:
            return self._unavailable("GraphHopper /match did not return JSON")

        paths = body.get("paths") or []
        if not paths:
            message = body.get("message") or "GraphHopper returned no matched path"
            return self._unavailable(str(message))
        path = paths[0]
        points = path.get("points")
        if not isinstance(points, dict) or points.get("type") != "LineString":
            return self._unavailable("GraphHopper response does not contain a LineString path")

        return {
            "available": True,
            "profile": profile,
            "geometry": points,
            "snapped_waypoints": path.get("snapped_waypoints") or body.get("snapped_waypoints"),
            "distance": path.get("distance"),
            "time": path.get("time"),
            "points_order": path.get("points_order") or [],
            "raw_response": {
                key: value
                for key, value in body.items()
                if key in {"info", "hints", "message"}
            },
        }

    @staticmethod
    def _unavailable(reason: str) -> dict[str, Any]:
        return {"available": False, "reason": reason}


def _observations_to_gpx(observations: list[dict[str, Any]]) -> str:
    track_points = []
    for observation in observations:
        lat = float(observation["lat"])
        lon = float(observation["lon"])
        timestamp = _gpx_time(observation.get("captured_at"))
        track_points.append(
            f'<trkpt lat="{lat:.8f}" lon="{lon:.8f}"><time>{escape(timestamp)}</time></trkpt>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="KI4Geodaten" xmlns="http://www.topografix.com/GPX/1/1">'
        "<trk><name>Mapillary sequence</name><trkseg>"
        + "".join(track_points)
        + "</trkseg></trk></gpx>"
    )


def _gpx_time(captured_at: Any) -> str:
    try:
        timestamp = float(captured_at) / 1000.0
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
