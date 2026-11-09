# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EGM
"""caflood_publish.py: output classification, COG conversion, manifest."""

from __future__ import annotations

import json
import time

import pytest

rasterio = pytest.importorskip("rasterio")
np = pytest.importorskip("numpy")

import caflood_publish  # noqa: E402
from caflood_publish import PUBLISH_GIVE_UP_S, PublishError, classify, main, publish  # noqa: E402
from caflood_status import CLAIM_FILE, FAILURES_FILE, RESULT_FILE  # noqa: E402
from conftest import DEM_CELL, DEM_CRS, DEM_XLL, DEM_YLL, EXECUTION_ID, STUDY_AREA_ID, config_text, write_asc  # noqa: E402

SHORT = EXECUTION_ID.rsplit(":", 1)[-1]


class FakeUploader:
    def __init__(self):
        self.uploaded = {}

    def upload(self, path):
        url = f"https://store/bucket/obj-{len(self.uploaded)}"
        self.uploaded[url] = path.read_bytes()
        return url

    def upload_many(self, paths):
        return {p: self.upload(p) for p in paths}


def grid(value_at):
    return [[round(value_at(r, c), 6) for c in range(20)] for r in range(10)]


@pytest.fixture
def run_dir(tmp_path):
    run = tmp_path / "run"
    out = run / "output_dir"
    out.mkdir(parents=True)
    (run / "config.csv").write_text(config_text(1800))
    (run / "job.json").write_text(json.dumps({"serviceExecutionId": EXECUTION_ID, "studyAreaId": STUDY_AREA_ID,
                                              "crs": DEM_CRS, "timeEndS": 1800}))
    geo = dict(xll=DEM_XLL, yll=DEM_YLL, cellsize=DEM_CELL)
    depth = lambda t: grid(lambda r, c: (r * 20 + c) * 0.001234 * t / 900)  # noqa: E731
    for t in (1800, 900):  # written out of order on purpose
        write_asc(out / f"{SHORT}_raster_WD_{t}.asc", depth(t), nodata=0, **geo)
        write_asc(out / f"{SHORT}_raster_VEL_V_{t}.asc", depth(t), nodata=0, **geo)
        write_asc(out / f"{SHORT}_raster_VEL_A_{t}.asc", depth(t), nodata=0, **geo)
    write_asc(out / f"{SHORT}_raster_WD_PEAK.asc", depth(1800), nodata=0, **geo)
    write_asc(out / f"{SHORT}_raster_VEL_V_PEAK.asc", depth(1800), nodata=0, **geo)
    write_asc(out / f"{SHORT}_TemporalOut_TIME.asc", grid(lambda r, c: 34.5 + r), nodata=-1, **geo)
    (out / f"{SHORT}_points_WD.csv.csv").write_text("Iter, Time (min), P1\n1,5,0.1\n")
    return run


def test_classify_skips_angles(run_dir):
    outputs, ignored = classify(run_dir / "output_dir")
    assert ignored == [f"{SHORT}_raster_VEL_A_1800.asc", f"{SHORT}_raster_VEL_A_900.asc"]
    kinds = {(o.kind, o.variable, o.t, o.peak) for o in outputs}
    assert len(kinds) == len(outputs) == 8  # 2x2 frames, 2 peaks, arrival, csv
    assert ("raster", "waterDepth", None, True) in kinds
    assert ("raster", "waterVelocity", 900.0, False) in kinds
    assert ("arrival", "floodArrivalTime", None, False) in kinds
    assert ("points", "points_WD", None, False) in kinds


def test_manifest(run_dir, tmp_path):
    work = tmp_path / "cog"
    work.mkdir()
    uploader = FakeUploader()
    result = publish(run_dir, work, uploader, None)
    manifest = result["manifest"]

    assert result["manifestUrl"] in uploader.uploaded
    assert json.loads(uploader.uploaded[result["manifestUrl"]]) == manifest
    assert (manifest["serviceExecutionId"], manifest["studyAreaId"], manifest["crs"]) == (EXECUTION_ID, STUDY_AREA_ID, DEM_CRS)
    assert manifest["bounds"] == [DEM_XLL, DEM_YLL, DEM_XLL + 100, DEM_YLL + 50]
    assert len(manifest["cornersWgs84"]) == 4
    lon_min, lat_min, lon_max, lat_max = manifest["boundsWgs84"]
    assert 7.4 < lon_min < lon_max < 7.6 and 43.7 < lat_min < lat_max < 43.9  # Menton

    wd = manifest["variables"]["waterDepth"]
    assert wd["unit"] == "m"
    assert [f["t"] for f in wd["frames"]] == [900.0, 1800.0]  # sorted
    assert set(wd["frames"][0]) == {"t", "url"}  # no "max"
    assert wd["peak"] in uploader.uploaded  # a plain URL
    assert manifest["variables"]["waterVelocity"]["unit"] == "m/s"
    assert set(manifest["variables"]["floodArrivalTime"]) == {"unit", "url"}
    assert "points_WD" in manifest["timeSeries"]
    assert "null" not in json.dumps(result)  # the broker refuses nulls
    assert len(uploader.uploaded) == 9  # 4 frames + 2 peaks + arrival + csv + manifest


def test_quality_code_and_warnings(run_dir, tmp_path):
    work = tmp_path / "cog"
    work.mkdir()
    (run_dir / "output.log").write_text("Simulation : x\n" + "!" * 53 + "\nCODE: 4\n" + "!" * 53 + "\n")
    result = publish(run_dir, work, FakeUploader(), None)
    assert result["qualityCode"] == 4
    assert any("edge of the domain" in w for w in result["warnings"])
    assert any("2 output file(s) not recognised" in w for w in result["warnings"])  # the _A_ angle grids


def test_missing_quality_code_is_a_warning(run_dir, tmp_path):
    work = tmp_path / "cog"
    work.mkdir()
    result = publish(run_dir, work, FakeUploader(), None)
    assert "qualityCode" not in result  # omitted, not null
    assert any("No CAFLOOD quality code" in w for w in result["warnings"])


def test_cog_content(run_dir, tmp_path):
    work = tmp_path / "cog"
    work.mkdir()
    uploader = FakeUploader()
    manifest = publish(run_dir, work, uploader, None)["manifest"]

    frame = manifest["variables"]["waterDepth"]["frames"][1]  # t = 1800
    cog = tmp_path / "frame.tif"
    cog.write_bytes(uploader.uploaded[frame["url"]])
    with rasterio.open(cog) as ds, rasterio.open(run_dir / "output_dir" / f"{SHORT}_raster_WD_1800.asc") as src:
        assert ds.crs.to_string() == DEM_CRS
        assert ds.dtypes[0] == "uint16" and ds.scales[0] == 0.001 and ds.nodata == 0
        assert np.abs(ds.read(1) * ds.scales[0] - src.read(1)).max() <= 0.0005

    arrival = tmp_path / "arrival.tif"
    arrival.write_bytes(uploader.uploaded[manifest["variables"]["floodArrivalTime"]["url"]])
    with rasterio.open(arrival) as ds:
        assert ds.dtypes[0] == "float32" and ds.read(1)[0, 0] == pytest.approx(34.5)


def test_missing_crs_needs_override(run_dir, tmp_path):
    (run_dir / "job.json").unlink()
    work = tmp_path / "cog"
    work.mkdir()
    with pytest.raises(PublishError, match="--crs"):
        publish(run_dir, work, FakeUploader(), None)
    assert publish(run_dir, work, FakeUploader(), DEM_CRS)["manifest"]["crs"] == DEM_CRS


# --- main(): finalize objects, claim, saved result, giving up ---------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    for key, value in {"STELLIO_BASE_URL": "http://x", "STELLIO_TOKEN_URL": "http://x",
                       "STELLIO_CLIENT_ID": "x", "STELLIO_CLIENT_SECRET": "x", "FILE_STORAGE_URL": "http://x",
                       "CAFLOOD_ENV_FILE": str(tmp_path / "none.env")}.items():
        monkeypatch.setenv(key, value)


class MainUploader(FakeUploader):
    """Stands in for caflood_publish.Uploader in main() (no network)."""

    instances = []

    def __init__(self, config, bucket, dry_run):
        super().__init__()
        self.cleaned = False
        MainUploader.instances.append(self)

    def cleanup(self):
        self.cleaned = True


@pytest.fixture
def fake_uploads(monkeypatch):
    MainUploader.instances.clear()
    monkeypatch.setattr(caflood_publish, "Uploader", MainUploader)


@pytest.fixture
def broken_publish(monkeypatch):
    def fail(*args, **kwargs):
        raise PublishError("upload: HTTP 500")
    monkeypatch.setattr(caflood_publish, "publish", fail)


def test_dry_run_prints_a_success_finalize_object(run_dir, env, capsys):
    assert main(["--local-dir", str(run_dir), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["action"], result["serviceExecutionId"]) == ("finalize", EXECUTION_ID)
    update = result["update"]
    assert (update["executionStatus"], update["progress"]) == ("success", 1)
    assert set(update["output"]) >= {"manifestUrl", "manifest", "warnings"}
    assert "qualityCode" not in update["output"]  # no CODE in this run: omitted, not null
    assert not (run_dir / RESULT_FILE).exists()  # dry run: no bookkeeping


def test_success_saves_the_result_and_releases_the_claim(run_dir, env, capsys, fake_uploads):
    assert main(["--local-dir", str(run_dir)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["update"]["executionStatus"] == "success"
    assert json.loads((run_dir / RESULT_FILE).read_text()) == printed["update"]  # re-sent if the PATCH fails
    assert not (run_dir / CLAIM_FILE).exists()


def test_failure_is_retried_until_giving_up(run_dir, env, capsys, fake_uploads, broken_publish):
    for attempt in (1, 2):
        assert main(["--local-dir", str(run_dir)]) == 1
        assert capsys.readouterr().out == ""  # nothing for NiFi: retried at the next poll
        assert json.loads((run_dir / FAILURES_FILE).read_text())["attempts"] == attempt
        assert not (run_dir / CLAIM_FILE).exists()  # released, so the next poll can retry
        assert MainUploader.instances[-1].cleaned  # partial uploads deleted

    # Failing for longer than PUBLISH_GIVE_UP_S: the run is closed with a failure.
    failures = json.loads((run_dir / FAILURES_FILE).read_text())
    failures["first"] = time.time() - PUBLISH_GIVE_UP_S - 60
    (run_dir / FAILURES_FILE).write_text(json.dumps(failures))
    assert main(["--local-dir", str(run_dir)]) == 0
    update = json.loads(capsys.readouterr().out)["update"]
    assert update["executionStatus"] == "failure"
    assert update["output"]["error"].startswith("Publishing the results kept failing for 61 min (3 attempts)")
    assert update["output"]["error"].endswith("upload: HTTP 500")
    assert json.loads((run_dir / RESULT_FILE).read_text()) == update
