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

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("MAPILLARY_ACCESS_TOKEN", "").strip() or None
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
        )
