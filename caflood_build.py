#!/usr/bin/env python3
"""
CAFLOOD pipeline, step 1 - build the input job for one ServiceExecution.

Called by NiFi (ExecuteStreamCommand) when Stellio notifies a CAFLOOD
ServiceExecution; the zip it streams out is then sent to the CAFLOOD VM
(PutSFTP), unzipped and launched. Steps 2 and 3 are caflood_poll.py and
caflood_publish.py.

  1. Reads the request (JSON) from stdin, in either shape (parse_payload):
     what Stellio notifies today - the ServiceExecution "input" alone,
     augmented by its creator with "serviceExecutionId" (full id) and
     "studyAreaId" - or the full envelope ("id", "entityId", "input").
     Examples in PIPELINE_REFERENCE.md section 2.
     No serviceName check: this endpoint only receives CAFLOOD executions.
  2. Fetches the StudyArea: its digitalElevationModelLinkUrl (object-store URL,
     fetched through the file-storage endpoint - or a local path for
     testing) and coordinateReferenceSystem.
  3. Turns the selected forcings into CAFLOOD events (caflood_inputs.py):
     relative timeframe = profiles; absolute = NOT IMPLEMENTED YET.
  4. Writes the job folder (caflood_setup_files.py): dem.asc, config.csv,
     event CSVs, raster / time-plot CSVs and job.json (execution id,
     StudyArea, CRS, Time End - read back by caflood_publish.py, since the
     ESRI ASCII outputs carry no CRS).
  5. Zips it and streams the zip bytes to stdout.

Job naming: folder, zip and CAFLOOD "Short Name" (output file prefix) are
the UUID part of the ServiceExecution id; the full id is the "Simulation
Name" (read back by caflood_poll.py).

Fixed CAFLOOD settings: model WCA2Dv2; velocity written as speed / angle
grids (Raster VEL Vector false) instead of the much larger CSV point cloud;
3 decimals in rasters. Tuning (Alpha, Max/Min DT, roughness, boundary
elevation...) comes from the optional input.tuningConfig, else
DEFAULT_TUNING (with a warning). Update DT is 60 s (CAFLOOD's maximum).

Contract with NiFi
  stdin   notification JSON (augmented input, or envelope) - flowfile content
  stdout  on success, the raw zip bytes ONLY (Output Destination: Flow File
          Content) - all diagnostics go to stderr
  exit 0  success; 1 input resolution / build failure; 2 bad payload or
          configuration (stdout empty in both failure cases)

Environment: see caflood_common.py (STELLIO_*, FILE_STORAGE_URL,
CAFLOOD_WORK_DIR, CAFLOOD_DATA_DIR). Dependencies: requests, pyproj.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

from caflood_common import (
    Config,
    ConfigError,
    FileStorageClient,
    FileStorageError,
    InputResolutionError,
    StellioClient,
)
from caflood_inputs import (
    CoordinateTransformer,
    DemGrid,
    ServiceExecutionPayload,
    collect_absolute_forcings,
    collect_relative_forcings,
    location_lonlat,
    parse_payload,
    parse_timeframe,
    read_attribute,
    resolve_input,
)
from caflood_setup_files import (
    EVENT_WRITERS,
    write_raster_grid_csv,
    write_setup_csv,
    write_time_plot_csv,
)

log = logging.getLogger("caflood_build")


# --------------------------------------------------------------------------
# Building the CAFLOOD job folder from a ServiceExecution
# --------------------------------------------------------------------------

# Fallback values used for any tuning key that input.tuningConfig omits (or
# when tuningConfig is absent entirely) - a warning names the defaulted keys.
DEFAULT_TUNING = dict(
    alpha=0.7,
    max_dt=5.0,
    min_dt=0.1,
    max_iter=2_000_000,
    roughness_global=0.03,
)

# Boundary Ele is always very negative to get an open boundary: CAFLOOD
# never drains the boundary ring, so its cells must be deep enough to absorb
# all outflow (0 fills up and water stacks at the edge). Must not equal the
# DEM nodata value. Any tuningConfig.boundaryElevation is ignored.
BOUNDARY_ELE = -10000.0

# Output period when requestedOutputs doesn't give one (outputRecoringPeriod).
DEFAULT_OUTPUT_PERIOD_S = 300.0
# CAFLOOD caps Update DT at 60 s anyway (DOCUMENTATION.md 5.2).
UPDATE_DT_S = 60.0

# input.tuningConfig key -> DEFAULT_TUNING/write_setup_csv key.
_TUNING_CONFIG_KEYS = {
    "alpha": "alpha",
    "maxDt": "max_dt",
    "minDt": "min_dt",
    "maxIteration": "max_iter",
    "roughnessGlobal": "roughness_global",
}


@dataclass
class BuildResult:
    work_dir: Path
    zip_path: Path
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_caflood_job(
    payload: ServiceExecutionPayload,
    stellio: StellioClient,
    storage: FileStorageClient,
    base_work_dir: Path,
    data_dir: Path,
) -> BuildResult:
    raw_input = payload.input
    # Job folder / zip / output prefix: the UUID part of the ServiceExecution
    # URN (the full URN is kept as the human-readable Simulation Name).
    short_name = _sanitize(payload.execution_id.rsplit(":", 1)[-1])
    job_dir = base_work_dir / short_name
    job_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    files: list[str] = []

    # --- StudyArea: DEM + CRS ------------------------------------------
    study_area = stellio.get_entity(payload.entity_id)
    dem_source = read_attribute(
        study_area, "digitalElevationModelLinkUrl", entity_id=payload.entity_id
    )
    crs = read_attribute(
        study_area, "coordinateReferenceSystem", entity_id=payload.entity_id
    )
    dem_filename = "dem.asc"
    materialize_file(
        str(dem_source), job_dir / dem_filename, storage=storage, data_dir=data_dir
    )
    files.append(dem_filename)
    dem = DemGrid.from_asc(job_dir / dem_filename)
    transform = CoordinateTransformer(str(crs))

    # --- timeframe ----------------------------------------------------------
    timeframe = parse_timeframe(raw_input, stellio)

    output_period_s = DEFAULT_OUTPUT_PERIOD_S
    update_dt = UPDATE_DT_S

    # --- rain / inflow / water-level forcings -----------------------------
    if timeframe.mode == "relative":
        events = collect_relative_forcings(
            raw_input, stellio, transform=transform, dem=dem
        )
    else:
        events = collect_absolute_forcings(
            raw_input, stellio, timeframe=timeframe, transform=transform, dem=dem
        )

    event_csvs: dict[str, list[str]] = {kind: [] for kind in EVENT_WRITERS}
    for i, event in enumerate(events):
        prefix, writer = EVENT_WRITERS[event.kind]
        filename = f"{prefix}_{i}.csv"
        writer(job_dir / filename, event)
        event_csvs[event.kind].append(filename)
        files.append(filename)

    # --- outputs --------------------------------------------------------
    raster_csvs: list[str] = []
    time_plot_csvs: list[str] = []
    requested_outputs = (
        resolve_input(raw_input.get("requestedOutputs"), stellio)
        if "requestedOutputs" in raw_input
        else None
    )
    if requested_outputs:
        raster_cfg = requested_outputs.get("rasterSpatialOutputs")
        if raster_cfg:
            period = raster_cfg.get("outputRecoringPeriod", output_period_s)
            for var in raster_cfg.get("requestedVariables", []):
                mapped = {"waterDepth": "WD", "waterVelocity": "VEL"}.get(var, var)
                filename = f"raster_{mapped}.csv"
                write_raster_grid_csv(
                    job_dir / filename,
                    name=f"{mapped}raster",
                    variable=mapped,
                    peak=True,
                    period_s=period,
                )
                raster_csvs.append(filename)
                files.append(filename)

        point_cfg = requested_outputs.get("pointMonitoringOutputs")
        if point_cfg:
            period = point_cfg.get("outputRecoringPeriod", output_period_s)
            points = []
            for cp_id in point_cfg.get("controlPoints", []):
                cp = stellio.get_entity(cp_id)
                x, y = transform(*location_lonlat(cp))
                points.append({"name": cp_id.rsplit(":", 1)[-1], "x": x, "y": y})
            for var in point_cfg.get("requestedVariables", []):
                mapped = {"waterDepth": "WD", "waterVelocity": "VEL"}.get(var, var)
                filename = f"points_{mapped}.csv"
                write_time_plot_csv(
                    job_dir / filename,
                    name=f"{mapped}points",
                    variable=mapped,
                    points=points,
                    period_s=period,
                )
                time_plot_csvs.append(filename)
                files.append(filename)
    else:
        # No requestedOutputs given at all - still produce sane default
        # peak-only WD/VEL rasters so the run isn't silent.
        warnings.append(
            "No requestedOutputs in input: defaulting to peak WD + VEL rasters only"
        )
        for var in ("WD", "VEL"):
            filename = f"raster_{var}.csv"
            write_raster_grid_csv(
                job_dir / filename,
                name=f"{var}raster",
                variable=var,
                peak=True,
                period_s=0,
            )
            raster_csvs.append(filename)
            files.append(filename)

    # --- tuning parameters ------------------------------------------------
    # Optional input.tuningConfig overrides DEFAULT_TUNING key by key; any
    # key it doesn't provide falls back to the default (and is warned about,
    # since a mismatched default here silently changes simulation behaviour).
    tuning = dict(DEFAULT_TUNING)
    tuning_config = resolve_input(raw_input.get("tuningConfig"), stellio) if "tuningConfig" in raw_input else None
    if tuning_config:
        missing = []
        for src_key, dest_key in _TUNING_CONFIG_KEYS.items():
            if src_key in tuning_config:
                tuning[dest_key] = tuning_config[src_key]
            else:
                missing.append(src_key)
        if missing:
            warnings.append(f"tuningConfig missing {missing}: using DEFAULT_TUNING for those keys")
        if "boundaryElevation" in tuning_config:
            warnings.append(f"tuningConfig.boundaryElevation ignored: Boundary Ele is always {BOUNDARY_ELE}")
    else:
        warnings.append(
            "No tuningConfig in input: using DEFAULT_TUNING placeholders for Alpha/"
            "Max-Min DT/Roughness"
        )

    # --- setup csv --------------------------------------------------------
    # Fixed name (not derived from the execution id): the CAFLOOD CLI takes
    # the setup file as an explicit argument, so its own filename is
    # arbitrary - "Short Name (for outputs)" inside it is what namespaces
    # the actual output artefacts.
    setup_filename = "config.csv"
    write_setup_csv(
        job_dir / setup_filename,
        short_name=short_name,
        sim_name=payload.execution_id,
        time_end_s=timeframe.duration_s,
        roughness_global=tuning["roughness_global"],
        boundary_ele=BOUNDARY_ELE,
        alpha=tuning["alpha"],
        max_dt=tuning["max_dt"],
        min_dt=tuning["min_dt"],
        update_dt=update_dt,
        max_iter=tuning["max_iter"],
        dem_filename=dem_filename,
        rain_event_csvs=event_csvs["rain"],
        inflow_event_csvs=event_csvs["inflow"],
        water_level_event_csvs=event_csvs["waterLevel"],
        time_plot_csvs=time_plot_csvs,
        raster_grid_csvs=raster_csvs,
    )
    files.append(setup_filename)

    # --- job metadata -------------------------------------------------------
    # Not read by CAFLOOD: lets caflood_publish.py georeference the output
    # rasters (ESRI ASCII grids carry no CRS) and label the manifest without
    # going back to the broker.
    job_filename = "job.json"
    (job_dir / job_filename).write_text(
        json.dumps(
            {
                "serviceExecutionId": payload.execution_id,
                "studyAreaId": payload.entity_id,
                "crs": str(crs),
                "timeEndS": timeframe.duration_s,
            },
            indent=2,
        )
    )
    files.append(job_filename)

    # --- zip ---------------------------------------------------------------
    zip_path = base_work_dir / f"{short_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(job_dir / name, arcname=name)

    return BuildResult(
        work_dir=job_dir, zip_path=zip_path, files=files, warnings=warnings
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _sanitize(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


def materialize_file(
    source: str, dest: Path, *, storage: FileStorageClient, data_dir: Path
) -> None:
    """Get a file "source" to `dest`. The source is either an object-store
    URL (https://<object-store>/<bucket>/<key>, as returned by an upload) -
    fetched through the file-storage endpoint, since the object store itself
    is not publicly readable - or a path to a file already sitting on this
    machine (local testing).
    """
    if source.startswith(("http://", "https://")):
        parts = urlparse(source).path.strip("/").split("/")
        if len(parts) < 2:
            raise InputResolutionError(
                f"File URL {source!r} is not of the form https://<host>/<bucket>/<key>"
            )
        dest.write_bytes(storage.fetch(parts[0], parts[-1]))
        return

    src_path = Path(source)
    if not src_path.is_absolute():
        src_path = data_dir / src_path
    if not src_path.is_file():
        raise InputResolutionError(
            f"File source {source!r} is neither an http(s) URL nor an existing local file "
            f"(resolved to {src_path}). Set CAFLOOD_DATA_DIR to the directory these "
            "relative paths should be resolved against."
        )
    shutil.copyfile(src_path, dest)



# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Read the input JSON from this file instead of stdin (for local testing)",
    )
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    raw = Path(args.input_file).read_text() if args.input_file else sys.stdin.read()
    try:
        payload = parse_payload(json.loads(raw))
    except json.JSONDecodeError as exc:
        log.error("stdin is not valid JSON: %s", exc)
        return 2
    except InputResolutionError as exc:
        log.error("Invalid payload: %s", exc)
        return 2

    # No serviceName to check here: Stellio only invokes this endpoint for
    # the CAFLOOD Service Registration, so receiving anything at all on
    # stdin means it's the right service.

    stellio = StellioClient(config)
    storage = FileStorageClient(config)
    try:
        config.work_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error(
            "Cannot create CAFLOOD_WORK_DIR '%s': %s. Check that it points to a "
            "directory the process actually has write access to (e.g. under the "
            "mounted/writable tree, not container-root paths like '/xxx').",
            config.work_dir,
            exc,
        )
        return 2

    try:
        result = build_caflood_job(
            payload, stellio, storage, config.work_dir, config.data_dir
        )
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    except InputResolutionError as exc:
        log.error("Failed to build CAFLOOD job: %s", exc)
        return 1
    except FileStorageError as exc:
        log.error("File storage request failed: %s", exc)
        return 1
    except requests.HTTPError as exc:
        log.error("Stellio request failed: %s", exc)
        return 1

    for warning in result.warnings:
        log.warning(warning)
    log.info(
        "Built CAFLOOD job for %s: %s (%s, %d files) -> streaming to stdout",
        payload.execution_id,
        result.zip_path,
        result.work_dir,
        len(result.files),
    )

    # stdout carries ONLY the raw zip bytes from here on - NiFi's
    # ExecuteStreamCommand (Output Destination = Flow File Content, the
    # default) turns that directly into the flowfile content, ready for
    # PutSFTP with no intermediate FetchFile/GetFile step. All diagnostics
    # stay on stderr via `log` above.
    with result.zip_path.open("rb") as fh:
        shutil.copyfileobj(fh, sys.stdout.buffer)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
