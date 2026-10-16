"""
Shared fixtures for the offline test suite: no network, no CAFLOOD VM, no
example files - everything (DEM, CAFLOOD log, run folders, entities) is
generated here, and Stellio / file storage are replaced by in-memory fakes.

Run from service_execution_test/:   python -m pytest tests
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from caflood_common import InputResolutionError  # noqa: E402

# --------------------------------------------------------------------------
# Synthetic DEM: 20 x 10 cells of 5 m in Lambert-93 (EPSG:2154), near Menton.
# Cell (row, col) elevation = row * 100 + col, so lookups are easy to check;
# cell (0, 0) is NODATA.
# --------------------------------------------------------------------------

DEM_CRS = "EPSG:2154"
DEM_XLL, DEM_YLL, DEM_CELL = 1061600.0, 6308600.0, 5.0
DEM_NCOLS, DEM_NROWS = 20, 10
DEM_NODATA = -99999
DEM_YTOP = DEM_YLL + DEM_NROWS * DEM_CELL


def dem_elevation(row: int, col: int) -> float:
    return float(row * 100 + col)


def write_asc(path: Path, rows: list[list[float]], *, xll: float, yll: float,
              cellsize: float, nodata: float) -> Path:
    header = (
        f"ncols {len(rows[0])}\nnrows {len(rows)}\nxllcorner {xll}\nyllcorner {yll}\n"
        f"cellsize {cellsize}\nNODATA_value {nodata}\n"
    )
    body = "\n".join(" ".join(str(v) for v in row) for row in rows)
    path.write_text(header + body + "\n")
    return path


@pytest.fixture
def dem_path(tmp_path: Path) -> Path:
    rows = [[dem_elevation(r, c) for c in range(DEM_NCOLS)] for r in range(DEM_NROWS)]
    rows[0][0] = DEM_NODATA
    return write_asc(tmp_path / "dem.asc", rows, xll=DEM_XLL, yll=DEM_YLL,
                     cellsize=DEM_CELL, nodata=DEM_NODATA)


def cell_center(row: int, col: int) -> tuple[float, float]:
    return DEM_XLL + (col + 0.5) * DEM_CELL, DEM_YTOP - (row + 0.5) * DEM_CELL


@pytest.fixture
def lonlat_of_cell():
    """WGS84 (lon, lat) of a DEM cell centre - what a frontend would send."""
    pyproj = pytest.importorskip("pyproj")
    to_wgs84 = pyproj.Transformer.from_crs(DEM_CRS, "EPSG:4326", always_xy=True)

    def convert(row: int, col: int) -> list[float]:
        return list(to_wgs84.transform(*cell_center(row, col)))

    return convert


# --------------------------------------------------------------------------
# Entities (shapes copied from the Stellio dev test entities)
# --------------------------------------------------------------------------

PROFILES = {
    "urn:ngsi-ld:InflowProfile:pulse": {
        "id": "urn:ngsi-ld:InflowProfile:pulse",
        "type": "InflowProfile",
        "name": {"type": "Property", "value": "Short pulse inflow"},
        "profile": {"type": "JsonProperty", "json": [
            {"time": 0, "inflow": 0}, {"time": 60, "inflow": 80},
            {"time": 180, "inflow": 80}, {"time": 240, "inflow": 0}]},
    },
    "urn:ngsi-ld:WaterLevelProfile:tail": {
        "id": "urn:ngsi-ld:WaterLevelProfile:tail",
        "type": "WaterLevelProfile",
        "name": {"type": "Property", "value": "Constant tailwater"},
        "profile": {"type": "JsonProperty", "json": [
            {"timeStop": 0, "waterLevel": 0}, {"timeStop": 60, "waterLevel": 30},
            {"timeStop": 540, "waterLevel": 30}, {"timeStop": 600, "waterLevel": 0}]},
    },
    "urn:ngsi-ld:RainfallProfile:burst": {
        "id": "urn:ngsi-ld:RainfallProfile:burst",
        "type": "RainfallProfile",
        "name": {"type": "Property", "value": "Short intense burst"},
        "profile": {"type": "JsonProperty", "json": [
            {"timeStop": 0, "rainIntensity": 0}, {"timeStop": 60, "rainIntensity": 10},
            {"timeStop": 120, "rainIntensity": 120}, {"timeStop": 180, "rainIntensity": 40},
            {"timeStop": 300, "rainIntensity": 0}]},
    },
}

STUDY_AREA_ID = "urn:ngsi-ld:StudyArea:test"
EXECUTION_ID = "urn:ngsi-ld:ServiceExecution:5be89d42-92c2-400b-b788-2fa59396d9bd"


class FakeStellio:
    def __init__(self, entities: dict[str, dict]):
        self.entities = entities
        self.calls: list[str] = []

    def get_entity(self, entity_id: str) -> dict:
        self.calls.append(entity_id)
        if entity_id not in self.entities:
            raise InputResolutionError(f"Entity not found in Stellio: {entity_id}")
        return copy.deepcopy(self.entities[entity_id])


class FakeStorage:
    def __init__(self, objects: dict[tuple[str, str], bytes] | None = None):
        self.objects = objects or {}
        self.fetched: list[tuple[str, str]] = []

    def fetch(self, bucket: str, key: str) -> bytes:
        self.fetched.append((bucket, key))
        return self.objects[(bucket, key)]


def study_area(dem_source: str) -> dict:
    return {
        "id": STUDY_AREA_ID,
        "type": "StudyArea",
        "digitalElevationModelLinkUrl": {"type": "Property", "value": dem_source},
        "coordinateReferenceSystem": {"type": "Property", "value": DEM_CRS},
    }


def relationship(object_id: str, lonlat: list[float] | None = None) -> dict:
    rel = {"type": "Relationship", "object": object_id}
    if lonlat is not None:
        rel["location"] = {"type": "GeoProperty", "value": {"type": "Point", "coordinates": lonlat}}
    return rel


def relative_execution(hydraulic: list[dict], rainfall: list[dict], duration: str = "PT36H") -> dict:
    return {
        "id": EXECUTION_ID,
        "type": "ServiceExecution",
        "entityId": STUDY_AREA_ID,
        "entityType": "StudyArea",
        "serviceName": "CAFLOOD",
        "input": {
            "timeframeControl": {"type": "Property", "value": "relative"},
            "hydraulicForcing": hydraulic,
            "rainfallForcing": rainfall,
            "initalWaterDepth": {"type": "Property", "value": "Skip"},
            "duration": {"type": "Property", "value": duration},
            "requestedOutputs": {"type": "JsonProperty", "json": {
                "rasterSpatialOutputs": {"requestedVariables": ["waterDepth", "waterVelocity"],
                                         "outputRecoringPeriod": 300}}},
        },
    }


def notification(execution: dict) -> dict:
    """What Stellio actually notifies: the input alone, augmented by the
    creator with serviceExecutionId / studyAreaId."""
    return {
        "serviceExecutionId": {"type": "Property", "value": execution["id"]},
        "studyAreaId": {"type": "Property", "value": execution["entityId"]},
        **execution["input"],
    }


# --------------------------------------------------------------------------
# Synthetic CAFLOOD run: config.csv + output.log in the real formats
# --------------------------------------------------------------------------

def config_text(time_end_s: float = 3600, name: str = EXECUTION_ID) -> str:
    short = name.rsplit(":", 1)[-1]
    return (
        f"Simulation Name,{name}\r\nShort Name (for outputs),{short}\r\nModel Type,WCA2Dv2\r\n"
        f"Time Start (seconds),0\r\nTime End (seconds),{time_end_s}\r\nUpdate DT (seconds),60.0\r\n"
    )


def caflood_log(time_end_s: int = 3600, period_s: int = 900) -> tuple[list[str], dict[str, int]]:
    """Lines of a complete CAFLOOD output.log, plus cut points (line counts)
    to simulate a run stopped in each phase."""
    lines: list[str] = []
    cuts: dict[str, int] = {}
    name = EXECUTION_ID
    short = name.rsplit(":", 1)[-1]
    add = lambda *ls: lines.extend(l + "\n" for l in ls)  # noqa: E731

    add(f"Pre-processing : {name}", "-" * 42, "Saved Grid information", "Saved Elevation data",
        "-----------------", "Total run time taken (s) = 0.009", "-----------------")
    cuts["preprocessing"] = len(lines)
    add(f"Simulation : {name}", "Model      : WCA2Dv2", "Date Start : 2026-10-1 14:14:29", "-" * 42,
        "Loaded Grid data", "Start main loop", "-----------------")
    for t in range(period_s, time_end_s + 1, period_s):
        add(f"Write Raster Grid (MIN {t // 60}):  WD WDPEAK VA VAPEAK", "-----",
            f"Total iterations = {t * 10} Simulation time (MIN) = {t // 60} Last DT = 0.1",
            "Last iterations  = 60 Average DT =5 Min DT = 5 Max DT = 5", "-----")
        if t == time_end_s // 2:
            cuts["mid_simulation"] = len(lines)
    add("-----------------", "Total run time taken (s) = 169.184", "-----------------",
        f"Simulation : {name}", "Model      : WCA2Dv2", "Date End   : 2026-10-1 14:17:18", "-" * 42,
        f"Write Time Raster Grid: output_dir/{short}_TemporalOut_TIME", "!" * 53, "CODE: 0", "!" * 53)
    cuts["end_of_simulation"] = len(lines)
    add(f"Post-processing : {name}", "-" * 42, "Loaded Grid data", "Loaded Elevation data")
    for t in range(period_s, time_end_s + 1, period_s):
        add(f"Write Raster Grid: output_dir/{short}_raster_WD_{t}",
            f"Write Raster Grid: output_dir/{short}_raster_VEL_V_{t}")
        if t == time_end_s // 2:
            cuts["mid_postprocessing"] = len(lines)
    add(f"Write Raster Grid: output_dir/{short}_raster_WD_PEAK",
        f"Write Raster Grid: output_dir/{short}_raster_VEL_V_PEAK",
        "Cleaned data", "-----------------", "Total run time taken (s) = 6.4", "-----------------")
    cuts["done"] = len(lines)
    return lines, cuts


def status_bundle(*, exit_code: str = "", alive: str = "yes", log_lines: list[str] | None = None,
                  time_end_s: float = 3600, claim_age: str = "", publish_result: str = "") -> str:
    """What caflood_poll.py's remote scan prints for one run folder."""
    log_lines = log_lines or []
    text = "".join(log_lines).encode()[-65536:].decode(errors="replace")
    code = "".join(l for l in log_lines if l.startswith("CODE:"))
    return (f"@@exit_code\n{exit_code}\n@@alive\n{alive}\n@@config\n{config_text(time_end_s)}\n"
            f"@@code\n{code}@@claim_age\n{claim_age}\n@@publish_result\n{publish_result}\n@@log\n{text}")
