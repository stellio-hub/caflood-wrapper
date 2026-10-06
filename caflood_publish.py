#!/usr/bin/env python3
"""
CAFLOOD pipeline, step 3 - publish the results of a finished run: pull its
output rasters from the CAFLOOD VM, convert them to Cloud-Optimised
GeoTIFFs, upload each one to the file storage, then upload a manifest
indexing them.

Called by NiFi for runs caflood_poll.py reports with action "publish". It
prints one "finalize" object, exactly like caflood_poll.py's - NiFi PATCHes
"update" to the ServiceExecution, then runs
`caflood_poll.py --mark-reported <folder>`:

  {"folder": "...", "serviceExecutionId": "urn:ngsi-ld:ServiceExecution:...",
   "action": "finalize",
   "update": {"executionStatus": "success", "progress": 1,
              "output": {"manifestUrl": "...", "manifest": {...},
                         "qualityCode": 0, "warnings": [...]}}}

Bookkeeping in the run folder (names in caflood_status.py):
  - "publishing" is created first: while it is fresh, caflood_poll.py
    reports "wait" for the run, so a slow publish isn't started twice; a
    claim older than CLAIM_TIMEOUT_S (crashed publisher) is ignored;
  - on failure, partial uploads are deleted, the attempt is counted in
    publish_failures.json and the claim removed: exit 1, nothing on stdout,
    the next poll retries. Once publishing has kept failing for
    PUBLISH_GIVE_UP_S (1 h), a "failure" finalize object is printed instead,
    so the run is closed rather than retried forever;
  - the final update (success, or that failure) is saved in
    publish_result.json: if the PATCH fails, the next poll re-sends it as
    "finalize" without publishing again.

  python caflood_publish.py <folder>
  python caflood_publish.py --local-dir <run folder> [--dry-run]   (testing)

What gets published (CAFLOOD output naming, setup written by
caflood_build.py - raster_WD.csv / raster_VEL.csv,
"Raster VEL Vector" false):
  <short>_raster_WD_<t>[.asc]      water depth at t (s)    -> COG uint16, mm
  <short>_raster_WD_PEAK[.asc]     peak water depth        -> COG uint16, mm
  <short>_raster_VEL_V_<t>[.asc]   speed at t              -> COG uint16, mm/s
  <short>_raster_VEL_V_PEAK[.asc]  peak speed              -> COG uint16, mm/s
  <short>_TemporalOut_TIME[.asc]   flood arrival time: simulation time (s)
                                   at which each cell's depth first exceeded
                                   CAFLOOD's threshold (timeToThreshold.ca) -
                                   NOT the time of the peak -> COG float32
  <short>_points_<VAR>.csv.csv     point time series       -> uploaded as-is
  *_A_* (velocity angles)          skipped
uint16 rasters carry scale 0.001 in their metadata, so GDAL / QGIS /
geotiff.js read real metres (m/s); 0 = dry / no data (CAFLOOD's own output
nodata value).

The rasters' CRS comes from job.json (written into the job by
caflood_build.py); runs built before that need --crs.

The manifest: serviceExecutionId, studyAreaId, crs, bounds (in crs),
boundsWgs84 and cornersWgs84 (clockwise from top-left, MapLibre image-source
order), timeEndS, then per variable (waterDepth, waterVelocity) a unit, a
"peak" URL and "frames" [{t, url}] sorted by t; floodArrivalTime {unit, url};
timeSeries {points_<VAR>: {url}}. Next to it in "output": qualityCode
(CAFLOOD's CODE 0/1/2/4) and warnings (quality code meaning, unrecognised
output files).

Environment: STELLIO_* (token), FILE_STORAGE_URL, FILE_STORAGE_OUTPUT_BUCKET,
CAFLOOD_SSH_* and CAFLOOD_RUNS_DIR - see caflood_common.py. Dependencies:
rasterio (ships GDAL), pyproj, requests.

Exit codes: 0 = a finalize object was printed (success, or failure after
PUBLISH_GIVE_UP_S of failures), 1 = attempt failed, retry later, 2 = bad
usage / configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from caflood_common import (
    Config,
    ConfigError,
    FileStorageClient,
    FileStorageError,
    check_folder_name,
    run_ssh,
    ssh_argv,
    ssh_settings,
)
from caflood_status import (
    CLAIM_FILE,
    FAILURES_FILE,
    QUALITY_CODE_WARNINGS,
    RESULT_FILE,
    compact,
    failure_update,
    service_execution_id,
    success_update,
)

DEFAULT_OUTPUT_BUCKET = "urbaquantum-caflood-outputs-rasters"
UPLOAD_WORKERS = 6
UINT16_SCALE = 0.001  # mm (depth) / mm/s (speed)
PUBLISH_GIVE_UP_S = 3600  # report a failure once publishing has kept failing this long
QUALITY_CODE_FILE = "quality_code"

_RASTER = re.compile(
    r"_raster_(?P<var>WD|VEL)(?P<speed>_V)?_(?P<t>PEAK|\d+(?:\.\d+)?)(?:\.asc)?$"
)
_ARRIVAL = re.compile(r"_TemporalOut_TIME(?:\.asc)?$")
_POINTS = re.compile(r"_points_(?P<var>WD|WL|VEL)\.csv\.csv$")
_CODE_LINE = re.compile(r"^CODE:\s*(\d+)", re.MULTILINE)

_VARIABLES = {
    "WD": ("waterDepth", "m"),
    "VEL": ("waterVelocity", "m/s"),
}

# Remote: run folder outputs + metadata as a gzipped tar on stdout. The
# quality "CODE:" line is extracted from output.log first (the log itself
# can be large).
_FETCH_SCRIPT = """\
cd {run_dir} || exit 3
(grep -a '^CODE:' output.log 2>/dev/null || true) > {code_file}
{{ find output_dir -maxdepth 1 -type f \\( -name '*.asc' -o -name '*.csv' \\) ! -name '*_A_*' -print0
  printf 'config.csv\\0{code_file}\\0'
  [ -f job.json ] && printf 'job.json\\0'
}} | tar czf - --null -T -
"""


class PublishError(Exception):
    pass


@dataclass
class Output:
    path: Path
    kind: str  # "raster" | "arrival" | "points"
    variable: str  # waterDepth / waterVelocity / floodArrivalTime / points_<VAR>
    t: float | None = None  # seconds, None for PEAK / arrival / points
    peak: bool = False


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def fetch_remote_run(folder: str, dest: Path) -> Path:
    check_folder_name(folder)
    settings = ssh_settings()
    run_dir = f"{settings['runs_dir']}/{folder}"
    script = _FETCH_SCRIPT.format(run_dir=shlex.quote(run_dir), code_file=QUALITY_CODE_FILE)
    # stderr to a file, not a pipe: see caflood_common.run_ssh (ControlPersist).
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(ssh_argv(settings, script), stdout=subprocess.PIPE, stderr=err)
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tar:
                _safe_extract(tar, dest)
        except tarfile.TarError as exc:
            proc.kill()
            raise PublishError(f"could not read the outputs archive from the VM: {exc}") from None
        finally:
            proc.stdout.close()
            proc.wait(timeout=60)
        err.seek(0)
        stderr = err.read().decode(errors="replace")
    if proc.returncode != 0:
        raise PublishError(f"ssh/tar exited with {proc.returncode}: {stderr.strip()[:500]}")
    return dest


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    try:
        tar.extractall(dest, filter="data")
    except TypeError:  # Python without extraction filters (< 3.11.4)
        for member in tar:
            target = (dest / member.name).resolve()
            if not member.isfile() or dest.resolve() not in target.parents:
                raise PublishError(f"unexpected entry in outputs archive: {member.name!r}")
            tar.extract(member, dest)


def read_quality_code(run_dir: Path) -> int | None:
    """CAFLOOD's CODE: from the extracted quality_code file (remote fetch) or
    straight from output.log (local run folder)."""
    for name in (QUALITY_CODE_FILE, "output.log"):
        path = run_dir / name
        if path.is_file():
            m = _CODE_LINE.search(path.read_text(errors="replace"))
            if m:
                return int(m.group(1))
    return None


# --------------------------------------------------------------------------
# Classification / conversion
# --------------------------------------------------------------------------


def classify(output_dir: Path) -> tuple[list[Output], list[str]]:
    outputs, ignored = [], []
    for path in sorted(output_dir.iterdir()):
        name = path.name
        if (m := _RASTER.search(name)):
            variable = _VARIABLES[m["var"]][0]
            if m["var"] == "VEL" and not m["speed"]:
                ignored.append(name)  # vector-CSV velocity: not published
                continue
            peak = m["t"] == "PEAK"
            outputs.append(Output(path, "raster", variable, None if peak else float(m["t"]), peak))
        elif _ARRIVAL.search(name):
            outputs.append(Output(path, "arrival", "floodArrivalTime"))
        elif (m := _POINTS.search(name)):
            outputs.append(Output(path, "points", f"points_{m['var']}"))
        else:
            ignored.append(name)
    return outputs, ignored


def to_cog(src: Path, dst: Path, *, crs: str, kind: str) -> tuple[float, float, float, float]:
    """Convert one ESRI ASCII grid to a COG; returns its bounds (in crs)."""
    import numpy as np
    import rasterio

    with rasterio.open(src) as ds:
        data = ds.read(1).astype("float32")
        profile = ds.profile
        bounds = ds.bounds
        src_nodata = ds.nodata
    valid = np.isfinite(data) if src_nodata is None else (np.isfinite(data) & (data != src_nodata))

    for key in ("blockxsize", "blockysize", "tiled", "interleave"):
        profile.pop(key, None)
    profile.update(driver="COG", crs=crs, compress="DEFLATE", count=1)

    if kind == "raster":
        encoded = np.where(valid, np.clip(np.round(data / UINT16_SCALE), 0, 65535), 0).astype("uint16")
        profile.update(dtype="uint16", nodata=0, predictor=2)
        scale = UINT16_SCALE
    else:  # arrival time: seconds, can exceed uint16
        nodata = src_nodata if src_nodata is not None else -9999.0
        encoded = np.where(valid, data, nodata).astype("float32")
        profile.update(dtype="float32", nodata=nodata, predictor=3)
        scale = None

    with rasterio.open(dst, "w", **profile) as out:
        out.write(encoded, 1)
        if scale is not None:
            out.scales = (scale,)
            out.offsets = (0.0,)
    return tuple(bounds)


def wgs84_extent(bounds: tuple[float, float, float, float], crs: str) -> dict[str, Any]:
    from pyproj import Transformer

    left, bottom, right, top = bounds
    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    # Clockwise from top-left: the corner order MapLibre image sources use.
    corners = [t.transform(x, y) for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))]
    lons, lats = zip(*corners)
    return {
        "boundsWgs84": [round(min(lons), 7), round(min(lats), 7), round(max(lons), 7), round(max(lats), 7)],
        "cornersWgs84": [[round(lon, 7), round(lat, 7)] for lon, lat in corners],
    }


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


class Uploader:
    """Parallel uploads, one FileStorageClient (HTTP session) per thread.
    Remembers what it uploaded, so a failed publish can be cleaned up."""

    def __init__(self, config: Config, bucket: str, dry_run: bool):
        self._config = config
        self._bucket = bucket
        self._dry_run = dry_run
        self._local = threading.local()
        self._lock = threading.Lock()
        self.uploaded: list[str] = []

    def _client(self) -> FileStorageClient:
        if not hasattr(self._local, "client"):
            self._local.client = FileStorageClient(self._config)
        return self._local.client

    def upload(self, path: Path) -> str:
        if self._dry_run:
            return f"dry-run://{path.name}"
        url = self._client().upload(self._bucket, path.read_bytes())
        with self._lock:
            self.uploaded.append(url)
        return url

    def upload_many(self, paths: list[Path]) -> dict[Path, str]:
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
            return dict(zip(paths, pool.map(self.upload, paths)))

    def cleanup(self) -> None:
        """Best effort: delete everything uploaded so far."""
        for url in self.uploaded:
            try:
                self._client().delete(self._bucket, url)
            except FileStorageError as exc:
                print(f"caflood_publish: could not delete {url}: {exc}", file=sys.stderr)
        self.uploaded.clear()


# --------------------------------------------------------------------------
# Publish
# --------------------------------------------------------------------------


def publish(run_dir: Path, work_dir: Path, uploader: Uploader, crs_override: str | None) -> dict[str, Any]:
    """Convert + upload a run's outputs; returns the success "output" object
    (manifestUrl, manifest, qualityCode, warnings)."""
    job = read_job(run_dir)
    crs = crs_override or job.get("crs")
    if not crs:
        raise PublishError("no job.json with a 'crs' in the run folder (run built by an older wrapper?) - pass --crs")

    output_dir = run_dir / "output_dir"
    if not output_dir.is_dir():
        raise PublishError(f"no output_dir in {run_dir}")
    outputs, ignored = classify(output_dir)
    if not any(o.kind != "points" for o in outputs):
        raise PublishError(f"no publishable rasters in {output_dir} (ignored: {ignored[:10]})")

    # Convert every raster, then upload everything in one parallel batch.
    files: dict[int, Path] = {}
    bounds = None
    for i, output in enumerate(outputs):
        if output.kind == "points":
            files[i] = output.path
        else:
            files[i] = work_dir / f"{i:05d}.tif"
            bounds = to_cog(output.path, files[i], crs=crs, kind=output.kind)
    urls = uploader.upload_many(list(files.values()))

    manifest: dict[str, Any] = {
        "serviceExecutionId": execution_id_of(run_dir, job),
        "studyAreaId": job.get("studyAreaId"),
        "crs": crs,
        "bounds": [round(b, 3) for b in bounds],
        **wgs84_extent(bounds, crs),
        "timeEndS": job.get("timeEndS"),
        "format": "COG GeoTIFF, uint16 with scale 0.001 (m, m/s) and nodata 0, "
                  "except floodArrivalTime (float32, seconds)",
        "variables": {},
    }
    variables = manifest["variables"]
    for i, output in enumerate(outputs):
        url = urls[files[i]]
        if output.kind == "raster":
            entry = variables.setdefault(output.variable, {"unit": dict(_VARIABLES.values())[output.variable],
                                                           "peak": None, "frames": []})
            if output.peak:
                entry["peak"] = url
            else:
                entry["frames"].append({"t": output.t, "url": url})
        elif output.kind == "arrival":
            variables["floodArrivalTime"] = {"unit": "s", "url": url}
        else:
            manifest.setdefault("timeSeries", {})[output.variable] = {"url": url, "format": "csv"}
    for entry in variables.values():
        if "frames" in entry:
            entry["frames"].sort(key=lambda f: f["t"])
        if entry.get("peak") is None:
            entry.pop("peak", None)  # no nulls for the broker
    manifest = {k: v for k, v in manifest.items() if v is not None}

    manifest_path = work_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    manifest_url = uploader.upload(manifest_path)

    quality_code = read_quality_code(run_dir)
    warnings = []
    if quality_code is None:
        warnings.append("No CAFLOOD quality code (CODE:) found in output.log")
    elif quality_code in QUALITY_CODE_WARNINGS:
        warnings.append(QUALITY_CODE_WARNINGS[quality_code])
    if ignored:
        warnings.append(f"{len(ignored)} output file(s) not recognised and not published: {ignored[:5]}")
    return compact({"manifestUrl": manifest_url, "manifest": manifest, "qualityCode": quality_code,
                    "warnings": warnings})


def read_job(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "job.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def execution_id_of(run_dir: Path | None, job: dict[str, Any], folder: str = "") -> str:
    if job.get("serviceExecutionId"):
        return job["serviceExecutionId"]
    config = run_dir / "config.csv" if run_dir else None
    config_text = config.read_text() if config and config.is_file() else ""
    return service_execution_id(folder or (run_dir.name if run_dir else ""), config_text)


class RunFolder:
    """Small bookkeeping files in a run folder: on the CAFLOOD VM over SSH,
    or in a local folder (--local-dir)."""

    def __init__(self, folder: str | None, local_dir: Path | None):
        self._local = local_dir
        if local_dir is None:
            check_folder_name(folder)
            self._settings = ssh_settings()
            self._dir = f"{self._settings['runs_dir']}/{folder}"

    def _remote(self, name: str) -> str:
        return shlex.quote(f"{self._dir}/{name}")

    def read(self, name: str) -> str:
        if self._local is not None:
            path = self._local / name
            return path.read_text() if path.is_file() else ""
        return run_ssh(self._settings, f"cat {self._remote(name)} 2>/dev/null || true")

    def write(self, name: str, text: str, *, then_remove: str | None = None) -> None:
        """Write `name`; optionally remove `then_remove` in the same call
        (one SSH command instead of two)."""
        if self._local is not None:
            (self._local / name).write_text(text)
            if then_remove:
                (self._local / then_remove).unlink(missing_ok=True)
        else:
            command = f"cat > {self._remote(name)}"
            if then_remove:
                command += f" && rm -f {self._remote(then_remove)}"
            run_ssh(self._settings, command, input_text=text)

    def touch(self, name: str) -> None:
        if self._local is not None:
            (self._local / name).touch()
        else:
            run_ssh(self._settings, f"touch {self._remote(name)}")

    def remove(self, name: str) -> None:
        if self._local is not None:
            (self._local / name).unlink(missing_ok=True)
        else:
            run_ssh(self._settings, f"rm -f {self._remote(name)}")


def record_failure(run_folder: RunFolder, now: float) -> tuple[float, int]:
    """Count a failed attempt in publish_failures.json; returns (seconds since
    the first failure, number of attempts). While still retrying, the claim
    is released in the same call, so the next poll can retry."""
    try:
        failures = json.loads(run_folder.read(FAILURES_FILE) or "{}")
    except ValueError:
        failures = {}
    failures = {"first": failures.get("first", now), "attempts": failures.get("attempts", 0) + 1}
    failing_for = now - failures["first"]
    retrying = failing_for < PUBLISH_GIVE_UP_S
    run_folder.write(FAILURES_FILE, json.dumps(failures), then_remove=CLAIM_FILE if retrying else None)
    return failing_for, failures["attempts"]


_BOOKKEEPING_ERRORS = (RuntimeError, subprocess.TimeoutExpired, OSError)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", nargs="?", help="Run folder name on the CAFLOOD VM (as listed by caflood_poll.py)")
    parser.add_argument("--local-dir", type=Path, help="Publish a local run folder instead (testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Convert but do not upload (URLs are placeholders, no bookkeeping files)")
    parser.add_argument("--crs", help="CRS of the output rasters, when the run has no job.json")
    parser.add_argument("--bucket", default=None, help="Output bucket (default: $FILE_STORAGE_OUTPUT_BUCKET)")
    args = parser.parse_args(argv)
    if bool(args.folder) == bool(args.local_dir):
        parser.error("give either a run folder name, or --local-dir")
    folder = args.folder or args.local_dir.name

    try:
        config = Config.from_env()
        run_folder = None if args.dry_run else RunFolder(args.folder, args.local_dir)
    except (ConfigError, ValueError) as exc:
        print(f"caflood_publish: {exc}", file=sys.stderr)
        return 2
    bucket = args.bucket or os.environ.get("FILE_STORAGE_OUTPUT_BUCKET") or DEFAULT_OUTPUT_BUCKET
    uploader = Uploader(config, bucket, args.dry_run)

    if run_folder is not None:
        try:
            run_folder.touch(CLAIM_FILE)  # the poller now reports "wait" for this run
        except _BOOKKEEPING_ERRORS as exc:
            print(f"caflood_publish: could not claim the run: {exc}", file=sys.stderr)
            return 1

    with tempfile.TemporaryDirectory(prefix="caflood_publish_") as tmp:
        tmp_path = Path(tmp)
        run_dir: Path | None = args.local_dir
        try:
            run_dir = args.local_dir or fetch_remote_run(args.folder, tmp_path / "run")
            work_dir = tmp_path / "cog"
            work_dir.mkdir()
            update = success_update(publish(run_dir, work_dir, uploader, args.crs))
        except Exception as exc:  # noqa: BLE001 - every failure counts as an attempt
            error = str(exc) if isinstance(exc, (PublishError, FileStorageError)) else f"{type(exc).__name__}: {exc}"
            print(f"caflood_publish: {error}", file=sys.stderr)
            uploader.cleanup()
            if run_folder is None:
                return 1
            try:
                failing_for, attempts = record_failure(run_folder, time.time())
                if failing_for < PUBLISH_GIVE_UP_S:
                    print(f"caflood_publish: attempt {attempts} failed (failing for {failing_for / 60:.0f} min, "
                          f"giving up after {PUBLISH_GIVE_UP_S / 60:.0f}) - will be retried", file=sys.stderr)
                    return 1
            except _BOOKKEEPING_ERRORS as book_exc:
                # The claim expires after CLAIM_TIMEOUT_S, then the run is retried.
                print(f"caflood_publish: could not record the failed attempt: {book_exc}", file=sys.stderr)
                return 1
            update = failure_update(
                1.0, f"Publishing the results kept failing for {failing_for / 60:.0f} min "
                     f"({attempts} attempts): {error}")
        execution_id = execution_id_of(run_dir, read_job(run_dir) if run_dir and run_dir.is_dir() else {}, folder)

        if run_folder is not None:
            # Saved so that a failed PATCH is retried without publishing again.
            try:
                run_folder.write(RESULT_FILE, json.dumps(update), then_remove=CLAIM_FILE)
            except _BOOKKEEPING_ERRORS as exc:
                print(f"caflood_publish: could not save the result in the run folder: {exc}", file=sys.stderr)
        if args.dry_run:
            keep = Path(tempfile.mkdtemp(prefix="caflood_publish_dry_"))
            for f in (tmp_path / "cog").glob("*"):
                f.replace(keep / f.name)
            print(f"caflood_publish: dry run, converted files kept in {keep}", file=sys.stderr)
    print(json.dumps({"folder": folder, "serviceExecutionId": execution_id, "action": "finalize", "update": update}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
