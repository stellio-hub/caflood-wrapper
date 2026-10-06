"""
Writers for the CAFLOOD setup files (used by caflood_build.py): config.csv
plus the rain / inflow / water-level event CSVs and the raster / time-plot
output CSVs. Formats per caflood/DOCUMENTATION.md.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from caflood_inputs import Area, ForcingEvent, Zone


# --------------------------------------------------------------------------
# CAFLOOD file writers (formats per DOCUMENTATION.md)
# --------------------------------------------------------------------------


def write_csv(path: Path, rows: list[list[Any]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerows(rows)


def _location_row(zone: Zone | None, area: Area | None) -> list[list[Any]]:
    if zone is not None:
        return [["Zone (tlx tly w h)", *zone]]
    if area is not None:
        return [["Area (tlx tly brx bry)", *area]]
    return []


def write_rain_event_csv(path: Path, event: ForcingEvent) -> None:
    # Location is genuinely optional for rain per DOCUMENTATION.md 6.2: with
    # neither Area nor Zone, CAFLOOD applies the event to the whole domain -
    # so the row is omitted entirely rather than written empty.
    rows = [
        ["Event Name", event.name],
        ["Rain Intensity (mm/hr)", *event.values],
        ["Time Stop (seconds)", *event.times_s],
        *_location_row(event.zone, event.area),
    ]
    write_csv(path, rows)


def write_inflow_event_csv(path: Path, event: ForcingEvent) -> None:
    rows = [
        ["Event Name", event.name],
        ["Inflow (cumecs)", *event.values],
        ["Time (seconds)", *event.times_s],
        *_location_row(event.zone, event.area),
    ]
    write_csv(path, rows)


def write_water_level_event_csv(path: Path, event: ForcingEvent) -> None:
    # event.values are already absolute water-surface elevations here.
    rows = [
        ["Event Name", event.name],
        ["Water Level (m)", *event.values],
        ["Time (seconds)", *event.times_s],
        *_location_row(event.zone, event.area),
    ]
    write_csv(path, rows)


# event kind -> (CSV filename prefix, writer)
EVENT_WRITERS = {
    "rain": ("rain", write_rain_event_csv),
    "inflow": ("inflow", write_inflow_event_csv),
    "waterLevel": ("waterlevel", write_water_level_event_csv),
}


def write_raster_grid_csv(
    path: Path, *, name: str, variable: str, peak: bool, period_s: float
) -> None:
    rows = [
        ["Raster Grid Name", name],
        ["Physical Variable", variable],
        ["Peak", str(peak).lower()],
        ["Period", period_s],
    ]
    write_csv(path, rows)


def write_time_plot_csv(
    path: Path,
    *,
    name: str,
    variable: str,
    points: list[dict[str, Any]],
    period_s: float,
) -> None:
    rows = [
        ["Time Plot Name", name],
        ["Physical Variable", variable],
        ["Points Name", *[p["name"] for p in points]],
        ["Points X Coo", *[p["x"] for p in points]],
        ["Points Y Coo", *[p["y"] for p in points]],
        ["Period", period_s],
    ]
    write_csv(path, rows)


def write_setup_csv(
    path: Path,
    *,
    short_name: str,
    sim_name: str,
    time_end_s: float,
    roughness_global: float,
    boundary_ele: float,
    alpha: float,
    max_dt: float,
    min_dt: float,
    update_dt: float,
    max_iter: int,
    dem_filename: str,
    rain_event_csvs: list[str],
    inflow_event_csvs: list[str],
    water_level_event_csvs: list[str],
    time_plot_csvs: list[str],
    raster_grid_csvs: list[str],
) -> None:
    def row(keyword: str, *values: Any) -> list[Any]:
        return [keyword, *values]

    rows = [
        row("Simulation Name", sim_name),
        row("Short Name (for outputs)", short_name),
        row("Model Type", "WCA2Dv2"),
        row("Time Start (seconds)", 0),
        row("Time End (seconds)", time_end_s),
        row("Max DT (seconds)", max_dt),
        row("Min DT (seconds)", min_dt),
        row("Update DT (seconds)", update_dt),
        row("Alpha (Fraction DT 0.0-1.0)", alpha),
        row("Max Iterations", max_iter),
        row("Roughness Global", roughness_global),
        row("Boundary Ele (Hi/Closed-Lo/Open)", boundary_ele),
        row("Elevation ASCII", dem_filename),
        row("Rain Event CSV", *rain_event_csvs),
        row("Inflow Event CSV", *inflow_event_csvs),
        row("Water Level Event CSV", *water_level_event_csvs),
        row("Time Plot CSV", *time_plot_csvs),
        row("Raster Grid CSV", *raster_grid_csvs),
        # Velocity as plain speed (_V_) / angle (_A_) ASCII grids instead of
        # the default X,Y,speed,3-angles CSV point cloud (~6x larger), and mm
        # precision instead of 6 decimals of float32 noise (~2x smaller).
        row("Raster VEL Vector", "false"),
        row("Raster Decimal Places", 3),
        row("Output Console", "true"),
        row("Output Computation Time", "true"),
        row("Check Volumes", "true"),
    ]
    write_csv(path, rows)


