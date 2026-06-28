from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mapillary_access_token: str | None
    cache_dir: Path
    cache_ttl_seconds: int
    max_images_per_grid: int
    database_url: str | None
    ollama_base_url: str | None
    ollama_model: str
    ollama_timeout_seconds: int
    ollama_max_images_per_request: int
    ollama_image_thumb_size: int
    ollama_concurrency: int = 4
    graphhopper_base_url: str | None = None
    graphhopper_timeout_seconds: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("MAPILLARY_ACCESS_TOKEN", "").strip() or None
        database_url = os.getenv("DATABASE_URL", "").strip() or None
        ollama_base_url = os.getenv("OLLAMA_BASE_URL", "").strip().rstrip("/") or None
        graphhopper_base_url = os.getenv("GRAPHHOPPER_BASE_URL", "").strip().rstrip("/") or None
        return cls(
            mapillary_access_token=token,
            cache_dir=Path(os.getenv("MAPILLARY_CACHE_DIR", "data/cache")),
            cache_ttl_seconds=int(
                os.getenv("MAPILLARY_CACHE_TTL_SECONDS", "86400")
            ),
            max_images_per_grid=min(
                int(os.getenv("MAPILLARY_MAX_IMAGES_PER_GRID", "2000")),
                2000,
            ),
            database_url=database_url,
            ollama_base_url=ollama_base_url,
            ollama_model=os.getenv("OLLAMA_MODEL", "gemma4:26b").strip(),
            ollama_timeout_seconds=int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180")),
            ollama_max_images_per_request=max(
                1, int(os.getenv("OLLAMA_MAX_IMAGES_PER_REQUEST", "20"))
            ),
            ollama_image_thumb_size=_ollama_image_thumb_size(),
            ollama_concurrency=max(1, int(os.getenv("OLLAMA_CONCURRENCY", "4"))),
            graphhopper_base_url=graphhopper_base_url,
            graphhopper_timeout_seconds=int(os.getenv("GRAPHHOPPER_TIMEOUT_SECONDS", "30")),
        )


def _ollama_image_thumb_size() -> int:
    value = int(os.getenv("OLLAMA_IMAGE_THUMB_SIZE", "512"))
    return value if value in {256, 512, 1024} else 512
