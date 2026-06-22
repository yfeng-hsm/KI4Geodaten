"""Build a compact Mainz-only Census 2022 grid dataset from official sources."""

from argparse import ArgumentParser
import csv
import json
import math
from pathlib import Path
import tempfile
from urllib.request import urlretrieve
import zipfile

from pyproj import Transformer
import shapefile
from shapely.geometry import box, mapping, shape
from shapely.ops import transform


POPULATION_URL = (
    "https://www.destatis.de/static/DE/zensus/gitterdaten/"
    "Zensus2022_Bevoelkerungszahl.zip"
)
BOUNDARIES_URL = (
    "https://www.destatis.de/static/DE/zensus/gitterdaten/"
    "Shapefile_Zensus2022.zip"
)
METRIC_SOURCES = {
    "average_age": (
        "https://www.destatis.de/static/DE/zensus/gitterdaten/Durchschnittsalter_in_Gitterzellen.zip",
        "Zensus2022_Durchschnittsalter_100m-Gitter.csv",
        "Durchschnittsalter",
    ),
    "foreigners_pct": (
        "https://www.destatis.de/static/DE/zensus/gitterdaten/Auslaenderanteil_in_Gitterzellen.zip",
        "Zensus2022_Anteil_Auslaender_100m-Gitter.csv",
        "AnteilAuslaender",
    ),
    "under_18_pct": (
        "https://www.destatis.de/static/DE/zensus/gitterdaten/Anteil_unter_18-jaehrige_in_Gitterzellen.zip",
        "Zensus2022_Anteil_unter_18_100m-Gitter.csv",
        "AnteilUnter18",
    ),
    "over_65_pct": (
        "https://www.destatis.de/static/DE/zensus/gitterdaten/Anteil_ab_65-jaehrige_in_Gitterzellen.zip",
        "Zensus2022_Anteil_ueber_65_100m-Gitter.csv",
        "AnteilUeber65",
    ),
    "average_household_size": (
        "https://www.destatis.de/static/DE/zensus/gitterdaten/Durchschnittliche_Haushaltsgroesse_in_Gitterzellen.zip",
        "Zensus2022_Durchschn_Haushaltsgroesse_100m-Gitter.csv",
        "DurchschnHHGroesse",
    ),
}
MAINZ_AGS = "07315000"
GRID_SIZE = 100


def download(url: str, destination: Path) -> None:
    if not destination.exists():
        urlretrieve(url, destination)


def load_mainz_boundary(archive: Path, workdir: Path):
    with zipfile.ZipFile(archive) as zipped:
        for name in zipped.namelist():
            if "VG250_GEM." in name:
                zipped.extract(name, workdir)

    reader = shapefile.Reader(str(workdir / "EPSG_25832" / "VG250_GEM.shp"))
    for shape_record in reader.iterShapeRecords():
        if shape_record.record.as_dict()["AGS"] == MAINZ_AGS:
            return shape(shape_record.shape.__geo_interface__)
    raise RuntimeError(f"Mainz AGS {MAINZ_AGS} not found")


def load_population(archive: Path, candidate_ids: set[str]) -> dict[str, int | None]:
    population: dict[str, int | None] = {}
    with zipfile.ZipFile(archive) as zipped:
        with zipped.open("Zensus2022_Bevoelkerungszahl_100m-Gitter.csv") as raw:
            rows = csv.DictReader((line.decode("utf-8-sig") for line in raw), delimiter=";")
            for row in rows:
                grid_id = row["GITTER_ID_100m"]
                if grid_id not in candidate_ids:
                    continue
                value = row["Einwohner"].strip()
                population[grid_id] = int(value) if value.lstrip("-").isdigit() and int(value) >= 0 else None
    return population


def load_metric(
    archive: Path,
    csv_name: str,
    value_column: str,
    candidate_ids: set[str],
) -> dict[str, tuple[float | None, str | None]]:
    values: dict[str, tuple[float | None, str | None]] = {}
    with zipfile.ZipFile(archive) as zipped:
        with zipped.open(csv_name) as raw:
            rows = csv.DictReader((line.decode("utf-8-sig") for line in raw), delimiter=";")
            for row in rows:
                grid_id = row["GITTER_ID_100m"]
                if grid_id not in candidate_ids:
                    continue
                raw_value = row[value_column].strip().replace(",", ".")
                try:
                    value = float(raw_value)
                except ValueError:
                    value = None
                note = row.get("werterlaeuternde_Zeichen", "").strip() or None
                values[grid_id] = value, note
    return values


def main(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        population_zip = workdir / "population.zip"
        boundaries_zip = workdir / "boundaries.zip"
        download(POPULATION_URL, population_zip)
        download(BOUNDARIES_URL, boundaries_zip)

        boundary_25832 = load_mainz_boundary(boundaries_zip, workdir)
        to_3035 = Transformer.from_crs("EPSG:25832", "EPSG:3035", always_xy=True)
        to_4326_from_25832 = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
        to_4326_from_3035 = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)
        boundary_3035 = transform(to_3035.transform, boundary_25832)
        boundary_4326 = transform(to_4326_from_25832.transform, boundary_25832)

        min_east, min_north, max_east, max_north = boundary_3035.bounds
        min_east = math.floor(min_east / GRID_SIZE) * GRID_SIZE
        min_north = math.floor(min_north / GRID_SIZE) * GRID_SIZE
        max_east = math.ceil(max_east / GRID_SIZE) * GRID_SIZE
        max_north = math.ceil(max_north / GRID_SIZE) * GRID_SIZE

        cells = []
        candidate_ids: set[str] = set()
        for north in range(min_north, max_north, GRID_SIZE):
            for east in range(min_east, max_east, GRID_SIZE):
                cell = box(east, north, east + GRID_SIZE, north + GRID_SIZE)
                intersection = boundary_3035.intersection(cell)
                if intersection.is_empty or intersection.area <= 0:
                    continue
                grid_id = f"CRS3035RES100mN{north}E{east}"
                candidate_ids.add(grid_id)
                cells.append((grid_id, east, north, intersection.area / (GRID_SIZE * GRID_SIZE)))

        population = load_population(population_zip, candidate_ids)
        metrics = {}
        for metric_name, (url, csv_name, value_column) in METRIC_SOURCES.items():
            archive = workdir / f"{metric_name}.zip"
            download(url, archive)
            metrics[metric_name] = load_metric(
                archive, csv_name, value_column, candidate_ids
            )
        features = []
        for grid_id, east, north, city_share in cells:
            corners = [
                (east, north),
                (east + GRID_SIZE, north),
                (east + GRID_SIZE, north + GRID_SIZE),
                (east, north + GRID_SIZE),
                (east, north),
            ]
            ring = [list(to_4326_from_3035.transform(*point)) for point in corners]
            metric_values = {
                metric_name: values.get(grid_id, (None, None))[0]
                for metric_name, values in metrics.items()
            }
            quality_flags = {
                metric_name: values[grid_id][1]
                for metric_name, values in metrics.items()
                if grid_id in values and values[grid_id][1]
            }
            features.append(
                {
                    "type": "Feature",
                    "id": grid_id,
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {
                        "grid_id": grid_id,
                        "resolution_m": GRID_SIZE,
                        "x_sw": east,
                        "y_sw": north,
                        "city_area_share": round(city_share, 5),
                        "population": population.get(grid_id),
                        "population_status": (
                            "reported" if population.get(grid_id) is not None else "not_reported"
                        ),
                        **metric_values,
                        "quality_flags": quality_flags,
                    },
                }
            )

        dataset = {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "city": "Mainz",
                "ags": MAINZ_AGS,
                "census_date": "2022-05-15",
                "grid_crs": "EPSG:3035",
                "grid_resolution_m": GRID_SIZE,
                "cell_count": len(features),
                "population_cell_count": sum(
                    feature["properties"]["population"] is not None for feature in features
                ),
                "metrics": ["population", *METRIC_SOURCES.keys()],
                "source": "Statistische Ämter des Bundes und der Länder, Zensus 2022",
                "license": "Datenlizenz Deutschland - Namensnennung - Version 2.0",
            },
        }
        boundary = {
            "type": "Feature",
            "id": MAINZ_AGS,
            "geometry": mapping(boundary_4326),
            "properties": {"name": "Mainz", "ags": MAINZ_AGS},
        }
        (output_dir / "mainz_census_100m.geojson").write_text(
            json.dumps(dataset, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        (output_dir / "mainz_boundary.geojson").write_text(
            json.dumps(boundary, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    main(arguments.output)
