from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import re
from typing import Any

import httpx

from app.config import Settings


CAPTURE_POSITIONS = {
    "vehicle_road",
    "pedestrian_road",
    "bicycle_road",
    "other_location",
    "uncertain",
}
SURFACE_MATERIALS = {
    "asphalt",
    "concrete",
    "paving_stones",
    "unpaved",
    "uncertain",
}
YES_NO_UNCERTAIN = {"yes", "no", "uncertain"}
VLM_FIELDS = (
    "capture_position",
    "surface_material",
    "traffic_signal",
    "bench",
    "waste_basket",
    "independent_bicycle_road",
    "independent_pedestrian_road",
)

VLM_PROMPT = """
你是城市街景图像标注助手。只分析图像中相机拍摄位置附近、画面正下方可见的地面和道路空间，不要推断远处区域。

请返回严格 JSON，不要使用 Markdown，不要添加解释文字。字段如下：

{
  "capture_position": "vehicle_road | pedestrian_road | bicycle_road | other_location | uncertain",
  "surface_material": "asphalt | concrete | paving_stones | unpaved | uncertain",
  "traffic_signal": "yes | no | uncertain",
  "bench": "yes | no | uncertain",
  "waste_basket": "yes | no | uncertain",
  "independent_bicycle_road": "yes | no | uncertain",
  "independent_pedestrian_road": "yes | no | uncertain",
  "confidence": 0.0,
  "reason": "一句简短中文理由"
}

判断规则：
- capture_position 表示拍摄者/相机最可能所在的位置。
- vehicle_road: 车行道、机动车道路或路肩。
- pedestrian_road: 人行道、步行路径、广场步行区域。
- bicycle_road: 自行车道或明确的自行车专用路径。
- other_location: 草地、建筑入口、停车场、私人院落等不属于以上三类的位置。
- uncertain: 画面不足以判断。
- surface_material 只关心画面正下方/最近处地面材料。
- asphalt: 沥青。
- concrete: 混凝土或大块整体水泥面。
- paving_stones: 铺装砖、石板、鹅卵石、联锁砖。
- unpaved: 土路、砂砾、草地、裸土。
- uncertain: 无法判断。
- traffic_signal / bench / waste_basket 只判断图像中是否可见对应设施。
- independent_bicycle_road 表示是否存在与机动车道物理或视觉上独立的自行车道路。
- independent_pedestrian_road 表示是否存在与机动车道物理或视觉上独立的人行道路。
- yes/no/uncertain 字段无法判断时必须返回 uncertain。
""".strip()

PROMPT_VERSION = "street-position-surface-assets-v2"


class OllamaConfigurationError(RuntimeError):
    pass


class OllamaAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class OllamaClient:
    settings: Settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.ollama_base_url)

    async def status(self) -> dict[str, Any]:
        if not self.settings.ollama_base_url:
            return {"configured": False, "connected": False}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(f"{self.settings.ollama_base_url}/api/tags")
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "configured": True,
                "connected": False,
                "base_url": self.settings.ollama_base_url,
                "model": self.settings.ollama_model,
                "error": exc.__class__.__name__,
            }

        models = [model.get("name") for model in data.get("models", [])]
        return {
            "configured": True,
            "connected": True,
            "base_url": self.settings.ollama_base_url,
            "model": self.settings.ollama_model,
            "image_thumb_size": self.settings.ollama_image_thumb_size,
            "model_available": self.settings.ollama_model in models,
            "models": models,
        }

    async def analyze_images(
        self,
        grid_id: str,
        images: list[dict[str, Any]],
        model: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.ollama_base_url:
            raise OllamaConfigurationError("OLLAMA_BASE_URL is not configured")

        limited_images = images[: self.settings.ollama_max_images_per_request]
        results = []
        async with httpx.AsyncClient(timeout=self.settings.ollama_timeout_seconds) as client:
            for image in limited_images:
                results.append(await self.analyze_one(client, image, model))

        return {
            "grid_id": grid_id,
            "model": model or self.settings.ollama_model,
            "prompt_version": PROMPT_VERSION,
            "requested_count": len(images),
            "analyzed_count": len(results),
            "truncated": len(images) > len(limited_images),
            "results": results,
        }

    async def analyze_one(
        self, client: httpx.AsyncClient, image: dict[str, Any], model: str | None = None
    ) -> dict[str, Any]:
        image_id = str(image.get("id") or image.get("properties", {}).get("id") or "")
        properties = image.get("properties", {})
        image_url = _image_url_for_analysis(properties, self.settings.ollama_image_thumb_size)
        geometry = image.get("geometry")
        selected_model = model or self.settings.ollama_model
        if not image_url:
            return {
                "image_id": image_id,
                "geometry": geometry,
                "ok": False,
                "model": selected_model,
                "prompt_version": PROMPT_VERSION,
                "error": "Missing Mapillary thumbnail URL",
            }

        try:
            image_response = await client.get(image_url)
            image_response.raise_for_status()
            image_base64 = base64.b64encode(image_response.content).decode("ascii")
            payload = {
                "model": selected_model,
                "messages": [
                    {
                        "role": "user",
                        "content": VLM_PROMPT,
                        "images": [image_base64],
                    }
                ],
                "stream": False,
            }
            response = await client.post(
                f"{self.settings.ollama_base_url}/api/chat",
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["message"]["content"]
            parsed = _parse_json_object(content)
            normalized = _normalize_result(parsed)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return {
                "image_id": image_id,
                "ok": False,
                "model": selected_model,
                "prompt_version": PROMPT_VERSION,
                "error": exc.__class__.__name__,
            }

        return {
            "image_id": image_id,
            "geometry": geometry,
            "ok": True,
            "model": selected_model,
            "prompt_version": PROMPT_VERSION,
            **normalized,
        }


def _parse_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("Ollama response did not contain JSON")
        return json.loads(match.group(0))


def _image_url_for_analysis(properties: dict[str, Any], preferred_size: int) -> str | None:
    preferred_key = f"thumb_{preferred_size}_url"
    fallback_keys = [preferred_key, "thumb_256_url", "thumb_1024_url"]
    for key in dict.fromkeys(fallback_keys):
        value = properties.get(key)
        if value:
            return str(value)
    return None


def _normalize_result(data: dict[str, Any]) -> dict[str, Any]:
    capture_position = str(data.get("capture_position", "uncertain")).strip()
    surface_material = str(data.get("surface_material", "uncertain")).strip()
    if capture_position not in CAPTURE_POSITIONS:
        capture_position = "uncertain"
    if surface_material not in SURFACE_MATERIALS:
        surface_material = "uncertain"
    yes_no_fields = {}
    for field in (
        "traffic_signal",
        "bench",
        "waste_basket",
        "independent_bicycle_road",
        "independent_pedestrian_road",
    ):
        value = str(data.get(field, "uncertain")).strip()
        yes_no_fields[field] = value if value in YES_NO_UNCERTAIN else "uncertain"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "capture_position": capture_position,
        "surface_material": surface_material,
        **yes_no_fields,
        "confidence": confidence,
        "reason": str(data.get("reason", "")).strip()[:300],
    }
