# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EGM
"""caflood_build.py: ServiceExecution -> CAFLOOD job folder + zip."""

from __future__ import annotations

import csv
import json
import zipfile

import pytest

from caflood_build import build_caflood_job, main
from caflood_common import InputResolutionError
from caflood_inputs import parse_payload
from conftest import (
    DEM_CELL,
    EXECUTION_ID,
    PROFILES,
    STUDY_AREA_ID,
    FakeStellio,
    FakeStorage,
    cell_center,
    notification,
    dem_elevation,
    relationship,
    relative_execution,
    study_area,
)

SHORT_NAME = EXECUTION_ID.rsplit(":", 1)[-1]


def read_rows(path):
    with path.open(newline="") as fh:
        return {row[0]: row[1:] for row in csv.reader(fh) if row}


@pytest.fixture
def execution(lonlat_of_cell):
    return relative_execution(
        hydraulic=[relationship("urn:ngsi-ld:InflowProfile:pulse", lonlat_of_cell(5, 4)),
                   relationship("urn:ngsi-ld:WaterLevelProfile:tail", lonlat_of_cell(2, 15))],
        rainfall=[relationship("urn:ngsi-ld:RainfallProfile:burst")],
    )


def build(tmp_path, execution, dem_source, storage=None):
    stellio = FakeStellio({**PROFILES, STUDY_AREA_ID: study_area(dem_source)})
    return build_caflood_job(parse_payload(execution), stellio, storage, tmp_path / "work", tmp_path)


def test_job_folder_and_zip(tmp_path, dem_path, execution):
    result = build(tmp_path, execution, dem_source="dem.asc")  # local path, relative to data_dir

    assert result.work_dir.name == SHORT_NAME
    assert result.files == ["dem.asc", "inflow_0.csv", "waterlevel_1.csv", "rain_2.csv",
                            "raster_WD.csv", "raster_VEL.csv", "config.csv", "job.json"]
    assert zipfile.ZipFile(result.zip_path).namelist() == result.files
    assert result.zip_path.name == f"{SHORT_NAME}.zip"


def test_config_csv(tmp_path, dem_path, execution):
    result = build(tmp_path, execution, dem_source="dem.asc")
    config = read_rows(result.work_dir / "config.csv")

    assert config["Simulation Name"] == [EXECUTION_ID]
    assert config["Short Name (for outputs)"] == [SHORT_NAME]
    assert config["Time End (seconds)"] == ["129600.0"]  # PT36H
    assert config["Update DT (seconds)"] == ["60.0"]
    assert config["Elevation ASCII"] == ["dem.asc"]
    assert config["Inflow Event CSV"] == ["inflow_0.csv"]
    assert config["Water Level Event CSV"] == ["waterlevel_1.csv"]
    assert config["Rain Event CSV"] == ["rain_2.csv"]
    assert config["Raster Grid CSV"] == ["raster_WD.csv", "raster_VEL.csv"]
    assert config["Raster VEL Vector"] == ["false"]
    assert config["Raster Decimal Places"] == ["3"]
    assert config["Roughness Global"] == ["0.03"]  # DEFAULT_TUNING without tuningConfig
    assert any("tuningConfig" in w for w in result.warnings)


def test_event_and_output_csvs(tmp_path, dem_path, execution):
    work = build(tmp_path, execution, dem_source="dem.asc").work_dir

    inflow = read_rows(work / "inflow_0.csv")
    assert inflow["Inflow (cumecs)"] == ["0.0", "80.0", "80.0", "0.0"]
    assert inflow["Time (seconds)"] == ["0.0", "60.0", "180.0", "240.0"]
    assert [float(v) for v in inflow["Zone (tlx tly w h)"]] == pytest.approx([*cell_center(5, 4), DEM_CELL, DEM_CELL])

    level = read_rows(work / "waterlevel_1.csv")
    ground = dem_elevation(2, 15)
    assert [float(v) for v in level["Water Level (m)"]] == [ground, ground + 30, ground + 30, ground]

    rain = read_rows(work / "rain_2.csv")
    assert rain["Rain Intensity (mm/hr)"] == ["0.0", "10.0", "120.0", "40.0", "0.0"]
    assert not any(k.startswith(("Zone", "Area")) for k in rain)  # whole domain

    raster = read_rows(work / "raster_VEL.csv")
    assert raster == {"Raster Grid Name": ["VELraster"], "Physical Variable": ["VEL"], "Peak": ["true"], "Period": ["300"]}


def test_job_json(tmp_path, dem_path, execution):
    work = build(tmp_path, execution, dem_source="dem.asc").work_dir
    assert json.loads((work / "job.json").read_text()) == {
        "serviceExecutionId": EXECUTION_ID, "studyAreaId": STUDY_AREA_ID,
        "crs": "EPSG:2154", "timeEndS": 129600.0,
    }


def test_flat_notification_builds_the_same_job(tmp_path, dem_path, execution):
    from_envelope = build(tmp_path / "a", execution, dem_source=str(dem_path))
    from_notification = build(tmp_path / "b", notification(execution), dem_source=str(dem_path))
    assert from_notification.files == from_envelope.files
    for name in from_envelope.files:
        assert (from_notification.work_dir / name).read_bytes() == (from_envelope.work_dir / name).read_bytes(), name


def test_dem_url_is_fetched_through_file_storage(tmp_path, dem_path, execution):
    url = "https://store.example.com/urbaquantum-caflood-inputs-dem/9ac32dde"
    storage = FakeStorage({("urbaquantum-caflood-inputs-dem", "9ac32dde"): dem_path.read_bytes()})
    result = build(tmp_path, execution, dem_source=url, storage=storage)
    assert storage.fetched == [("urbaquantum-caflood-inputs-dem", "9ac32dde")]
    assert (result.work_dir / "dem.asc").read_bytes() == dem_path.read_bytes()


def test_missing_local_dem_fails(tmp_path, execution):
    with pytest.raises(InputResolutionError, match="neither an http"):
        build(tmp_path, execution, dem_source="nowhere/dem.asc")


def test_absolute_timeframe_not_implemented(tmp_path, dem_path, execution):
    execution["input"]["timeframeControl"]["value"] = "absolute"
    execution["input"]["timeStart"] = {"type": "Property", "value": "2026-07-03T22:00:00Z"}
    execution["input"]["timeEnd"] = {"type": "Property", "value": "2026-07-04T22:00:00Z"}
    with pytest.raises(InputResolutionError, match="not implemented"):
        build(tmp_path, execution, dem_source="dem.asc")


def test_cli_rejects_bad_payload(tmp_path, monkeypatch, capsys):
    for key, value in {"STELLIO_BASE_URL": "http://x", "STELLIO_TOKEN_URL": "http://x",
                       "STELLIO_CLIENT_ID": "x", "STELLIO_CLIENT_SECRET": "x", "FILE_STORAGE_URL": "http://x",
                       "CAFLOOD_WORK_DIR": str(tmp_path), "CAFLOOD_ENV_FILE": str(tmp_path / "none.env")}.items():
        monkeypatch.setenv(key, value)
    payload = tmp_path / "payload.json"
    payload.write_text('{"input": {}}')
    assert main([str(payload)]) == 2
    assert capsys.readouterr().out == ""  # nothing on stdout: it would become the flowfile
