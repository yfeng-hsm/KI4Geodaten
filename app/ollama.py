from __future__ import annotations

import base64
from io import BytesIO
from dataclasses import dataclass
import json
import re
from typing import Any

import httpx
from PIL import Image

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
    "sett",
    "unpaved",
    "uncertain",
}
YES_NO_UNCERTAIN = {"yes", "no", "uncertain"}
UNUSABLE_REASONS = {
    "none",
    "poor_image_quality",
    "transit_vehicle",
    "railway_scene",
    "uncertain",
}
VLM_FIELDS = (
    "unusable_reason",
    "capture_position",
    "surface_material",
    "surface_material_candidates",
    "left_sidewalk",
    "left_sidewalk_surface_material",
    "left_sidewalk_surface_material_candidates",
    "right_sidewalk",
    "right_sidewalk_surface_material",
    "right_sidewalk_surface_material_candidates",
    "left_adjacent_road_type",
    "left_adjacent_road_surface_material",
    "left_adjacent_road_surface_material_candidates",
    "right_adjacent_road_type",
    "right_adjacent_road_surface_material",
    "right_adjacent_road_surface_material_candidates",
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
  "unusable_reason": "none | poor_image_quality | transit_vehicle | railway_scene | uncertain",
  "capture_position": "vehicle_road | pedestrian_road | bicycle_road | other_location | uncertain",
  "surface_material": "asphalt | concrete | paving_stones | sett | unpaved | uncertain",
  "surface_material_candidates": {"asphalt | concrete | paving_stones | sett | unpaved": 1.0},
  "left_sidewalk": "yes | no | uncertain | null",
  "left_sidewalk_surface_material": "asphalt | concrete | paving_stones | sett | unpaved | uncertain | null",
  "left_sidewalk_surface_material_candidates": {"asphalt | concrete | paving_stones | sett | unpaved": 1.0} | null,
  "right_sidewalk": "yes | no | uncertain | null",
  "right_sidewalk_surface_material": "asphalt | concrete | paving_stones | sett | unpaved | uncertain | null",
  "right_sidewalk_surface_material_candidates": {"asphalt | concrete | paving_stones | sett | unpaved": 1.0} | null,
  "left_adjacent_road_type": "vehicle_road | pedestrian_road | bicycle_road | none | uncertain | null",
  "left_adjacent_road_surface_material": "asphalt | concrete | paving_stones | sett | unpaved | uncertain | null",
  "left_adjacent_road_surface_material_candidates": {"asphalt | concrete | paving_stones | sett | unpaved": 1.0} | null,
  "right_adjacent_road_type": "vehicle_road | pedestrian_road | bicycle_road | none | uncertain | null",
  "right_adjacent_road_surface_material": "asphalt | concrete | paving_stones | sett | unpaved | uncertain | null",
  "right_adjacent_road_surface_material_candidates": {"asphalt | concrete | paving_stones | sett | unpaved": 1.0} | null,
  "traffic_signal": "yes | no | uncertain",
  "bench": "yes | no | uncertain",
  "waste_basket": "yes | no | uncertain",
  "independent_bicycle_road": "yes | no | uncertain",
  "independent_pedestrian_road": "yes | no | uncertain",
  "confidence": 0.0,
  "reason": "一句简短中文理由，必须说明 surface_material 的可见证据"
}

判断规则：
- 如果图像过暗、过曝、严重模糊、运动模糊、雨雪/污渍遮挡、镜头被遮挡、压缩伪影严重、画面主要是天空/车身/墙面/近距离物体，导致无法可靠判断拍摄位置或正下方路面，unusable_reason 返回 poor_image_quality，capture_position 返回 other_location，surface_material 返回 uncertain，其它道路邻接字段返回 null。
- 如果图像明显是在火车、电车、轻轨或其它轨道交通车辆上/车厢内拍摄，unusable_reason 返回 transit_vehicle，capture_position 返回 other_location，surface_material 返回 uncertain，其它道路邻接字段返回 null。
- 如果图像主要是铁轨、站台轨道区、铁路设施或无法对应街道通行空间，unusable_reason 返回 railway_scene，capture_position 返回 other_location，surface_material 返回 uncertain，其它道路邻接字段返回 null。
- 只有图像清晰且可以用于街道/人行道/自行车道空间分析时，unusable_reason 返回 none。
- 不要为了覆盖特殊位置而强行猜测；图像证据不足时优先返回 poor_image_quality 或 uncertain，并把 surface_material 返回 uncertain。
- capture_position 表示拍摄者/相机最可能所在的位置。
- vehicle_road: 车行道、机动车道路或路肩。
- pedestrian_road: 人行道、步行路径、广场步行区域。
- bicycle_road: 自行车道或明确的自行车专用路径。
- other_location: 草地、建筑入口、停车场、私人院落等不属于以上三类的位置。
- uncertain: 画面不足以判断。
- surface_material 只关心画面正下方/最近处地面材料。
- asphalt: 沥青。
- concrete: 混凝土或大块整体水泥面。
- paving_stones: 人工块材、混凝土砖、联锁砖、砖块或平整石板铺面；关键特征是工厂化/人工块材外观、顶面较平整、块材形状高度一致，表面整体连续且适合平稳通行。
- sett: 由切割过的天然石块形成的铺面，也可称小方石/石块铺面；石块可以规则排列、紧密排列，也可以形成扇形/弧形图案。sett 的关键不是一定宽缝或不规则，而是切割天然石块、小石块/方石/长方石、天然石材质感、边缘和顶面略有不平。注意：sett 不是完全未切割的鹅卵石/乱石；如果石头完全未切割、非常圆凸或极不规则，应更接近 unpaved/uncertain，而不是 sett。
- unpaved: 土路、砂砾、草地、裸土。
- uncertain: 无法判断。
- sett 与 paving_stones 必须对称比较，不要预设更倾向其中任何一种。先分别寻找两类证据，再根据可见证据强弱决定主标签。
- sett 的证据：切割天然石块、小方石/小长方石；天然石材质感；顶面或边缘略有不平；扇形/弧形小石块铺装；石块虽可规则紧密排列，但仍呈天然石块外观。
- paving_stones 的证据：人工混凝土砖、联锁砖、普通砖、规则工厂化块材或平整板材；块材尺寸和形状高度一致；顶面很平整；整体像人工预制铺装系统。
- 不要仅因为“排列规则、紧密、缝隙窄、图案连续”就排除 sett；欧洲街道中规则扇形/弧形的小型天然石块铺装通常更接近 sett，除非能明显看出是人工混凝土砖、联锁砖或平整板材。
- 马路边缘的一排路缘石、curbstone、边界石或排水石不应被识别为 paving_stones；只有画面正下方/最近处实际通行表面由密集铺装砖或石板构成时，才返回 paving_stones。
- reason 必须写出 surface_material 的可见证据，不要只写“根据图像判断”。如果 surface_material 是 sett 或 paving_stones，reason 必须提到至少两个证据维度：材料/块材类型、铺装图案、表面平整度、块材是否呈天然石或人工预制外观，并说明为什么不是另一类。
- 如果 surface_material_candidates 包含 sett 和 paving_stones，reason 必须说明两者分别有哪些证据，以及为什么主标签更合理。
- left_sidewalk/right_sidewalk 只在 capture_position 为 vehicle_road 时判断，以相机朝向为前方，左/右分别表示画面前进方向的左侧/右侧是否存在人行道。
- 如果 capture_position 不是 vehicle_road，left_sidewalk、right_sidewalk 和对应 surface_material 必须返回 null。
- 如果车行道左侧或右侧没有人行道，对应 sidewalk 字段返回 no，surface_material 返回 null。
- 如果存在人行道但材质无法判断，对应 sidewalk 字段返回 yes，surface_material 返回 uncertain。
- sidewalk surface_material 只描述人行道通行面，不要把单排路缘石/curbstone 当作 paving_stones。
- left_adjacent_road_type/right_adjacent_road_type 只在 capture_position 为 pedestrian_road 或 bicycle_road 时判断，以相机朝向为前方，左/右表示当前道路两侧是否紧邻其它通行道路。
- 如果 capture_position 不是 pedestrian_road 或 bicycle_road，left_adjacent_road_type、right_adjacent_road_type 和对应 surface_material 必须返回 null。
- 如果当前行人道或自行车道一侧紧邻车行道，adjacent_road_type 返回 vehicle_road，并提取该车行道可见通行面的 surface_material。
- 如果当前行人道一侧紧邻自行车道，adjacent_road_type 返回 bicycle_road，并提取该自行车道可见通行面的 surface_material。
- 如果当前自行车道一侧紧邻人行道，adjacent_road_type 返回 pedestrian_road，并提取该人行道可见通行面的 surface_material。
- 如果一侧没有紧邻车行道、自行车道或人行道，adjacent_road_type 返回 none，surface_material 返回 null。
- 如果能看到相邻道路但类型或材质无法判断，类型或材质分别返回 uncertain。
- traffic_signal / bench / waste_basket 只判断图像中是否可见对应设施。
- independent_bicycle_road 表示是否存在与机动车道物理或视觉上独立的自行车道路。
- independent_pedestrian_road 表示是否存在与机动车道物理或视觉上独立的人行道路。
- yes/no/uncertain 字段无法判断时必须返回 uncertain。
""".strip()

PROMPT_VERSION = "street-position-surface-assets-sidewalk-adjacent-quality-v15"


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
        image_properties = _stored_image_properties(properties)
        selected_model = model or self.settings.ollama_model
        if not image_url:
            return {
                "image_id": image_id,
                "geometry": geometry,
                "image_properties": image_properties,
                "ok": False,
                "model": selected_model,
                "prompt_version": PROMPT_VERSION,
                "error": "Missing Mapillary thumbnail URL",
            }

        try:
            image_response = await client.get(image_url)
            image_response.raise_for_status()
            image_bytes = _resize_image_for_analysis(
                image_response.content,
                self.settings.ollama_image_thumb_size,
            )
            image_base64 = base64.b64encode(image_bytes).decode("ascii")
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
                "geometry": geometry,
                "image_properties": image_properties,
                "ok": False,
                "model": selected_model,
                "prompt_version": PROMPT_VERSION,
                "error": exc.__class__.__name__,
            }

        return {
            "image_id": image_id,
            "geometry": geometry,
            "image_properties": image_properties,
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
    fallback_keys = [preferred_key]
    if preferred_size == 512:
        fallback_keys.extend(["thumb_1024_url", "thumb_256_url"])
    else:
        fallback_keys.extend(["thumb_256_url", "thumb_1024_url"])
    for key in dict.fromkeys(fallback_keys):
        value = properties.get(key)
        if value:
            return str(value)
    return None


def _resize_image_for_analysis(content: bytes, max_size: int) -> bytes:
    if max_size >= 1024:
        return content
    with Image.open(BytesIO(content)) as image:
        image.thumbnail((max_size, max_size))
        output = BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=85, optimize=True)
        return output.getvalue()


def _stored_image_properties(properties: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "captured_at",
        "camera_type",
        "height",
        "width",
        "thumb_256_url",
        "thumb_1024_url",
        "mapillary_url",
        "compass_angle",
        "computed_compass_angle",
        "original_geometry",
        "computed_geometry",
        "sequence_id",
    )
    return {key: properties.get(key) for key in keys if properties.get(key) is not None}


def _normalize_result(data: dict[str, Any]) -> dict[str, Any]:
    unusable_reason = str(data.get("unusable_reason", "none")).strip()
    if unusable_reason not in UNUSABLE_REASONS:
        unusable_reason = "uncertain"
    capture_position = str(data.get("capture_position", "uncertain")).strip()
    surface_material = str(data.get("surface_material", "uncertain")).strip()
    if capture_position not in CAPTURE_POSITIONS:
        capture_position = "uncertain"
    if surface_material not in SURFACE_MATERIALS:
        surface_material = "uncertain"
    if unusable_reason != "none":
        capture_position = "other_location"
        surface_material = "uncertain"
    surface_candidates = _normalize_surface_candidates(
        data.get("surface_material_candidates"),
        surface_material,
    )
    if _is_equal_sett_paving_stones_vote(surface_candidates):
        surface_material = "uncertain"
        surface_candidates = {}
    sidewalk_fields = _normalize_sidewalk_fields(data, capture_position)
    adjacent_road_fields = _normalize_adjacent_road_fields(data, capture_position)
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
        "unusable_reason": unusable_reason,
        "capture_position": capture_position,
        "surface_material": surface_material,
        "surface_material_candidates": surface_candidates,
        **sidewalk_fields,
        **adjacent_road_fields,
        **yes_no_fields,
        "confidence": confidence,
        "reason": str(data.get("reason", "")).strip()[:300],
    }


def _normalize_sidewalk_fields(data: dict[str, Any], capture_position: str) -> dict[str, str | None]:
    if capture_position != "vehicle_road":
        return {
            "left_sidewalk": None,
            "left_sidewalk_surface_material": None,
            "left_sidewalk_surface_material_candidates": None,
            "right_sidewalk": None,
            "right_sidewalk_surface_material": None,
            "right_sidewalk_surface_material_candidates": None,
        }

    normalized: dict[str, str | None] = {}
    for side in ("left", "right"):
        sidewalk_key = f"{side}_sidewalk"
        surface_key = f"{side}_sidewalk_surface_material"
        sidewalk = str(data.get(sidewalk_key, "uncertain")).strip()
        if sidewalk not in YES_NO_UNCERTAIN:
            sidewalk = "uncertain"
        surface = str(data.get(surface_key, "uncertain")).strip()
        if sidewalk == "yes":
            normalized[surface_key] = surface if surface in SURFACE_MATERIALS else "uncertain"
            normalized[f"{surface_key}_candidates"] = _normalize_surface_candidates(
                data.get(f"{surface_key}_candidates"),
                normalized[surface_key],
            )
            if _is_equal_sett_paving_stones_vote(normalized[f"{surface_key}_candidates"]):
                normalized[surface_key] = "uncertain"
                normalized[f"{surface_key}_candidates"] = {}
        else:
            normalized[surface_key] = None
            normalized[f"{surface_key}_candidates"] = None
        normalized[sidewalk_key] = sidewalk
    return normalized


def _normalize_adjacent_road_fields(data: dict[str, Any], capture_position: str) -> dict[str, str | None]:
    if capture_position not in {"pedestrian_road", "bicycle_road"}:
        return {
            "left_adjacent_road_type": None,
            "left_adjacent_road_surface_material": None,
            "left_adjacent_road_surface_material_candidates": None,
            "right_adjacent_road_type": None,
            "right_adjacent_road_surface_material": None,
            "right_adjacent_road_surface_material_candidates": None,
        }

    allowed_types = {"vehicle_road", "pedestrian_road", "bicycle_road", "none", "uncertain"}
    normalized: dict[str, str | None] = {}
    for side in ("left", "right"):
        type_key = f"{side}_adjacent_road_type"
        surface_key = f"{side}_adjacent_road_surface_material"
        road_type = str(data.get(type_key, "uncertain")).strip()
        if road_type not in allowed_types:
            road_type = "uncertain"
        surface = str(data.get(surface_key, "uncertain")).strip()
        if road_type in {"vehicle_road", "pedestrian_road", "bicycle_road"}:
            normalized[surface_key] = surface if surface in SURFACE_MATERIALS else "uncertain"
            normalized[f"{surface_key}_candidates"] = _normalize_surface_candidates(
                data.get(f"{surface_key}_candidates"),
                normalized[surface_key],
            )
            if _is_equal_sett_paving_stones_vote(normalized[f"{surface_key}_candidates"]):
                normalized[surface_key] = "uncertain"
                normalized[f"{surface_key}_candidates"] = {}
        else:
            normalized[surface_key] = None
            normalized[f"{surface_key}_candidates"] = None
        normalized[type_key] = road_type
    return normalized


def _normalize_surface_candidates(value: Any, primary: str | None) -> dict[str, float]:
    candidates: dict[str, float] = {}
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, dict):
                material = item.get("material") or item.get("surface") or item.get("value")
                weight = item.get("weight") or item.get("confidence") or item.get("probability")
                items.append((material, weight))
    else:
        items = []

    for material, weight in items:
        key = str(material).strip()
        if key not in SURFACE_MATERIALS or key == "uncertain":
            continue
        try:
            numeric = float(weight)
        except (TypeError, ValueError):
            continue
        if numeric <= 0:
            continue
        candidates[key] = candidates.get(key, 0.0) + numeric

    total = sum(candidates.values())
    if total > 0:
        return {
            material: round(weight / total, 4)
            for material, weight in sorted(candidates.items())
        }
    if primary in SURFACE_MATERIALS and primary != "uncertain":
        return {str(primary): 1.0}
    return {}


def _is_equal_sett_paving_stones_vote(candidates: dict[str, float]) -> bool:
    if set(candidates) != {"sett", "paving_stones"}:
        return False
    return abs(float(candidates["sett"]) - float(candidates["paving_stones"])) < 0.001
