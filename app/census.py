import json
from pathlib import Path


class CensusStore:
    def __init__(self, data_dir: Path):
        self.dataset = json.loads(
            (data_dir / "mainz_census_100m.geojson").read_text(encoding="utf-8")
        )
        self.boundary = json.loads(
            (data_dir / "mainz_boundary.geojson").read_text(encoding="utf-8")
        )
        self.cells = {feature["id"]: feature for feature in self.dataset["features"]}

    @property
    def meta(self) -> dict:
        return self.dataset["meta"]

    def cell(self, grid_id: str) -> dict | None:
        return self.cells.get(grid_id)
