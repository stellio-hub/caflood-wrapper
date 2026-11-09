# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EGM
"""
ServiceExecution input -> CAFLOOD forcing events (used by caflood_build.py).

  - parse_payload: the ServiceExecution envelope ({"id", "entityId", "input"});
  - resolve_input: NGSI-LD / CIM-055 input values (Property, JsonProperty,
    GeoProperty, EntityAttributeSelector, Relationship, lists) -> plain values;
  - parse_timeframe: "relative" (ISO-8601 duration) or "absolute"
    (timeStart / timeEnd);
  - DemGrid / CoordinateTransformer / place_location: WGS84 forcing
    locations -> one-cell CAFLOOD Zone (Point) or Area (Polygon bbox) in the
    DEM's CRS;
  - collect_relative_forcings: InflowProfile / WaterLevelProfile /
    RainfallProfile selections -> ForcingEvent list. The entity TYPE says
    what a profile is (not the input field it came from), the time key may
    be "timeStop" or "time", and the same profile may be selected several
    times. WaterLevelProfile values are depths above ground: the DEM
    elevation of the location's cell is added to get CAFLOOD's absolute
    Water Level.
  - collect_absolute_forcings: ControlPoint / WeatherInformation temporal
    queries - NOT IMPLEMENTED YET.

Dependency: pyproj (imported lazily, by CoordinateTransformer).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from caflood_common import ConfigError, InputResolutionError, StellioClient


# --------------------------------------------------------------------------
# Generic ETSI GR CIM-055 input resolution
# (Property / JsonProperty / GeoProperty / EntityAttributeSelector /
# RelationshipAttributeSelector / Relationship, and lists thereof)
# --------------------------------------------------------------------------


def resolve_input(value: Any, stellio: StellioClient) -> Any:
    """Resolve one entry of ServiceExecution.input to a plain Python value.

    Handles the selector types defined in the CIM-055 draft (see
    CIM(26)000021r1 clause 6.3.5.1) plus plain literals and lists.
    """
    if isinstance(value, list):
        return [resolve_input(item, stellio) for item in value]

    if not isinstance(value, dict) or "type" not in value:
        # Already a plain literal.
        return value

    kind = value["type"]

    if kind in ("Property", "GeoProperty"):
        return value["value"]

    if kind == "JsonProperty":
        return value.get("json")

    if kind == "EntityAttributeSelector":
        entity = stellio.get_entity(value["entityId"])
        return read_attribute(
            entity, value["attributeName"], entity_id=value["entityId"]
        )

    if kind == "RelationshipAttributeSelector":
        raise InputResolutionError(
            "RelationshipAttributeSelector resolution needs the *target* entity "
            "id of the ServiceExecution, which this generic resolver does not "
            "carry - resolve it inline where the target entity is already known."
        )

    if kind == "Relationship":
        # Keep the object id plus any sibling attributes (e.g. the forcing
        # "location" GeoProperty), the caller decides what to do with them.
        extra = {
            k: resolve_input(v, stellio)
            for k, v in value.items()
            if k not in ("type", "object")
        }
        return {"entityId": value["object"], **extra}

    raise InputResolutionError(f"Unsupported input value type: {kind!r}")


def read_attribute(
    entity: dict[str, Any], attribute_name: str, *, entity_id: str
) -> Any:
    if attribute_name not in entity:
        raise InputResolutionError(
            f"Entity {entity_id} has no attribute '{attribute_name}' "
            f"(available: {sorted(k for k in entity if k not in ('id', 'type'))})"
        )
    attr = entity[attribute_name]
    if isinstance(attr, dict):
        if "value" in attr:
            return attr["value"]
        if "json" in attr:  # JsonProperty, e.g. *Profile.profile
            return attr["json"]
    return attr


def entity_type(entity: dict[str, Any]) -> str:
    """Short entity type, whether or not the broker compacted it (without a
    matching @context Stellio returns the expanded IRI, e.g.
    https://uri.etsi.org/ngsi-ld/default-context/InflowProfile)."""
    return re.split(r"[/#]", str(entity.get("type", "")))[-1]


# --------------------------------------------------------------------------
# ServiceExecution envelope
# --------------------------------------------------------------------------


@dataclass
class ServiceExecutionPayload:
    execution_id: str  # ServiceExecution URN
    entity_id: str  # target StudyArea URN
    input: dict[str, Any]


_EXECUTION_PREFIX = "urn:ngsi-ld:ServiceExecution:"


def parse_payload(raw: Any) -> ServiceExecutionPayload:
    """Accepts both shapes a CAFLOOD request can arrive in:

    - the ServiceExecution envelope: {"id", "entityId", "input": {...}};
    - the notification Stellio actually sends today, i.e. the "input" object
      alone, augmented by its creator with "serviceExecutionId" (the full
      ServiceExecution id) and "studyAreaId" (the target StudyArea id), each
      a Property or a plain string. A bare UUID as serviceExecutionId is
      completed with the urn:ngsi-ld:ServiceExecution: prefix.
    """
    if not isinstance(raw, dict):
        raise InputResolutionError("Payload must be a JSON object")

    if isinstance(raw.get("input"), dict):  # envelope
        execution_id, entity_id, inputs = raw.get("id"), raw.get("entityId"), raw["input"]
        keys = ("id", "entityId")
    else:  # flat, augmented input
        execution_id = _plain_value(raw.get("serviceExecutionId"))
        entity_id = _plain_value(raw.get("studyAreaId"))
        inputs = raw
        keys = ("serviceExecutionId", "studyAreaId")
    for key, value in zip(keys, (execution_id, entity_id)):
        if not value:
            raise InputResolutionError(f"Missing '{key}' in the received payload")

    execution_id = str(execution_id)
    if not execution_id.startswith("urn:"):
        execution_id = _EXECUTION_PREFIX + execution_id
    return ServiceExecutionPayload(execution_id=execution_id, entity_id=str(entity_id), input=inputs)


def _plain_value(value: Any) -> Any:
    """{"type": "Property", "value": x} -> x; plain values unchanged."""
    if isinstance(value, dict) and value.get("type") == "Property":
        return value.get("value")
    return value


# --------------------------------------------------------------------------
# DEM grid + WGS84 -> DEM CRS placement of forcing locations
# --------------------------------------------------------------------------


@dataclass
class DemGrid:
    """Header of an ESRI ASCII Grid (DOCUMENTATION.md 6.1), plus on-demand
    lookup of single cell values - the grid body is never loaded whole."""

    path: Path
    ncols: int
    nrows: int
    xll: float  # lower-left corner (not centre) of the grid
    yll: float
    cellsize: float
    nodata: float | None
    header_lines: int

    @classmethod
    def from_asc(cls, path: Path) -> "DemGrid":
        header: dict[str, float] = {}
        with path.open() as fh:
            for line in fh:
                tokens = line.split()
                if len(tokens) != 2 or not tokens[0][0].isalpha():
                    break
                header[tokens[0].lower()] = float(tokens[1])
        try:
            ncols, nrows = int(header["ncols"]), int(header["nrows"])
            cellsize = header["cellsize"]
            if "xllcenter" in header:
                xll = header["xllcenter"] - cellsize / 2
                yll = header["yllcenter"] - cellsize / 2
            else:
                xll, yll = header["xllcorner"], header["yllcorner"]
        except KeyError as exc:
            raise InputResolutionError(
                f"DEM {path.name} has an incomplete ESRI ASCII header (missing {exc})"
            ) from None
        return cls(
            path=path,
            ncols=ncols,
            nrows=nrows,
            xll=xll,
            yll=yll,
            cellsize=cellsize,
            nodata=header.get("nodata_value"),
            header_lines=len(header),
        )

    @property
    def ytop(self) -> float:
        return self.yll + self.nrows * self.cellsize

    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        """(row, col) of the cell containing (x, y); row 0 is the top row."""
        col = math.floor((x - self.xll) / self.cellsize)
        row = math.floor((self.ytop - y) / self.cellsize)
        if not (0 <= col < self.ncols and 0 <= row < self.nrows):
            raise InputResolutionError(
                f"Point ({x:.2f}, {y:.2f}) is outside the DEM extent "
                f"x=[{self.xll}, {self.xll + self.ncols * self.cellsize}] "
                f"y=[{self.yll}, {self.ytop}]"
            )
        return row, col

    def cell_center(self, row: int, col: int) -> tuple[float, float]:
        return (
            self.xll + (col + 0.5) * self.cellsize,
            self.ytop - (row + 0.5) * self.cellsize,
        )

    def elevation_at(self, row: int, col: int) -> float:
        # Stream tokens rather than assuming one grid row per text line.
        target = row * self.ncols + col
        seen = 0
        with self.path.open() as fh:
            for _ in range(self.header_lines):
                next(fh)
            for line in fh:
                tokens = line.split()
                if seen + len(tokens) > target:
                    value = float(tokens[target - seen])
                    break
                seen += len(tokens)
            else:
                raise InputResolutionError(
                    f"DEM {self.path.name} ends before cell (row={row}, col={col})"
                )
        if self.nodata is not None and value == self.nodata:
            raise InputResolutionError(
                f"DEM cell (row={row}, col={col}) is NODATA - no ground elevation there"
            )
        return value


class CoordinateTransformer:
    """NGSI-LD GeoProperties are always WGS84 lon/lat (GeoJSON order);
    CAFLOOD zones/areas/points must be in the DEM's projected CRS."""

    def __init__(self, target_crs: str):
        try:
            from pyproj import Transformer
            from pyproj.exceptions import CRSError
        except ImportError:
            raise ConfigError(
                "pyproj is required to reproject forcing locations (pip install pyproj)"
            ) from None
        try:
            self._transformer = Transformer.from_crs(
                "EPSG:4326", target_crs, always_xy=True
            )
        except CRSError as exc:
            raise InputResolutionError(
                f"Unsupported coordinateReferenceSystem {target_crs!r}: {exc}"
            ) from None

    def __call__(self, lon: float, lat: float) -> tuple[float, float]:
        return self._transformer.transform(lon, lat)


Zone = tuple[float, float, float, float]  # (tlx, tly, w, h)
Area = tuple[float, float, float, float]  # (tlx, tly, brx, bry)


@dataclass
class Placement:
    zone: Zone | None = None
    area: Area | None = None
    cell: tuple[int, int] | None = None  # (row, col), Point locations only


def place_location(
    geometry: dict[str, Any], *, transform: CoordinateTransformer, dem: DemGrid
) -> Placement:
    """Turn a WGS84 GeoJSON geometry into a CAFLOOD Zone (Point) or Area
    (Polygon / MultiPolygon bounding box) in DEM coordinates."""
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")

    if geom_type == "Point":
        row, col = dem.cell_of(*transform(coords[0], coords[1]))
        # CAFLOOD resolves a Zone to the cell containing (tlx, tly) plus
        # ceil(w/cellsize) x ceil(h/cellsize) cells (Inflow/Rain/WaterLevel
        # .cpp) - anchoring on the cell centre with w = h = cellsize targets
        # exactly that one cell, with no ambiguity at cell edges.
        cx, cy = dem.cell_center(row, col)
        return Placement(
            zone=(cx, cy, dem.cellsize, dem.cellsize), cell=(row, col)
        )

    if geom_type in ("Polygon", "MultiPolygon"):
        rings = coords if geom_type == "Polygon" else [r for p in coords for r in p]
        xs, ys = zip(*(transform(lon, lat) for ring in rings for lon, lat, *_ in ring))
        return Placement(area=(min(xs), max(ys), max(xs), min(ys)))

    raise InputResolutionError(f"Unsupported location geometry type: {geom_type!r}")


# --------------------------------------------------------------------------
# Timeframe
# --------------------------------------------------------------------------


@dataclass
class Timeframe:
    mode: str  # "relative" | "absolute"
    duration_s: float
    start: datetime | None = None  # absolute mode only
    end: datetime | None = None


def parse_timeframe(raw_input: dict[str, Any], stellio: StellioClient) -> Timeframe:
    mode = resolve_input(raw_input.get("timeframeControl"), stellio)

    if mode == "relative":
        if "duration" not in raw_input:
            raise InputResolutionError("timeframeControl='relative' requires 'duration'")
        duration = parse_iso_duration(resolve_input(raw_input["duration"], stellio))
        timeframe = Timeframe(mode=mode, duration_s=duration.total_seconds())
    elif mode == "absolute":
        if "timeStart" not in raw_input or "timeEnd" not in raw_input:
            raise InputResolutionError(
                "timeframeControl='absolute' requires 'timeStart' and 'timeEnd'"
            )
        start = parse_iso(resolve_input(raw_input["timeStart"], stellio))
        end = parse_iso(resolve_input(raw_input["timeEnd"], stellio))
        timeframe = Timeframe(
            mode=mode, duration_s=(end - start).total_seconds(), start=start, end=end
        )
    else:
        raise InputResolutionError(
            f"Unsupported timeframeControl {mode!r} (expected 'relative' or 'absolute')"
        )

    if timeframe.duration_s <= 0:
        raise InputResolutionError(f"Simulation duration must be > 0 ({timeframe})")
    return timeframe


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_ISO_DURATION = re.compile(
    r"P(?!$)(?:(?P<weeks>\d+(?:\.\d+)?)W)?(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?=\d)(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?"
)


def parse_iso_duration(text: str) -> timedelta:
    """ISO-8601 duration with weeks/days/hours/minutes/seconds, e.g. PT36H,
    P3D, P1DT12H. Years and months are rejected: they have no fixed length
    in seconds."""
    m = _ISO_DURATION.fullmatch(str(text).strip())
    if not m:
        raise InputResolutionError(
            f"Unsupported ISO-8601 duration: {text!r} "
            "(expected e.g. PT36H, P3D, P1DT12H - years/months are not supported)"
        )
    return timedelta(**{k: float(v) for k, v in m.groupdict().items() if v})


# --------------------------------------------------------------------------
# Forcings -> CAFLOOD boundary events
# --------------------------------------------------------------------------


@dataclass
class ForcingEvent:
    kind: str  # "rain" | "inflow" | "waterLevel"
    name: str
    times_s: list[float]
    values: list[float]
    zone: Zone | None = None
    area: Area | None = None


# Profile entity type -> (event kind, accepted value keys in profile.json).
# WaterLevelProfile values are depths above ground (see module docstring),
# hence "waterDepth" is accepted alongside the historical "waterLevel" key.
_PROFILE_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "RainfallProfile": ("rain", ("rainIntensity",)),
    "InflowProfile": ("inflow", ("inflow", "Inflow")),
    "WaterLevelProfile": ("waterLevel", ("waterDepth", "waterLevel")),
}
_PROFILE_TIME_KEYS = ("timeStop", "time")


def extract_profile_series(
    entries: list[dict[str, Any]], value_keys: tuple[str, ...], *, entity_id: str
) -> tuple[list[float], list[float]]:
    if not entries:
        raise InputResolutionError(f"{entity_id}: profile.json is empty")

    def pick(entry: dict[str, Any], keys: tuple[str, ...]) -> float:
        key = next((k for k in keys if k in entry), None)
        if key is None:
            raise InputResolutionError(
                f"{entity_id}: profile entry {entry!r} has none of {list(keys)}"
            )
        return float(entry[key])

    times = [pick(entry, _PROFILE_TIME_KEYS) for entry in entries]
    values = [pick(entry, value_keys) for entry in entries]
    return times, values


def collect_relative_forcings(
    raw_input: dict[str, Any],
    stellio: StellioClient,
    *,
    transform: CoordinateTransformer,
    dem: DemGrid,
) -> list[ForcingEvent]:
    """One ForcingEvent per selected profile. Which input field a profile
    came from doesn't matter - its entity type says what it is. The same
    profile may legitimately be selected several times (e.g. at different
    locations), so selections are NOT de-duplicated."""
    selections: list[Any] = []
    for field_name in ("hydraulicForcing", "rainfallForcing"):
        if field_name in raw_input:
            selections.extend(as_list(resolve_input(raw_input[field_name], stellio)))

    entities: dict[str, dict[str, Any]] = {}
    events: list[ForcingEvent] = []
    for item in selections:
        profile_id = as_entity_id(item)
        location = item.get("location") if isinstance(item, dict) else None
        if profile_id not in entities:
            entities[profile_id] = stellio.get_entity(profile_id)
        entity = entities[profile_id]

        profile_type = entity_type(entity)
        if profile_type not in _PROFILE_TYPES:
            raise InputResolutionError(
                f"{profile_id}: unsupported forcing entity type {profile_type!r} "
                f"for a relative timeframe (expected one of {list(_PROFILE_TYPES)})"
            )
        kind, value_keys = _PROFILE_TYPES[profile_type]
        name = (
            read_attribute(entity, "name", entity_id=profile_id)
            if "name" in entity
            else profile_id
        )
        times, values = extract_profile_series(
            read_attribute(entity, "profile", entity_id=profile_id),
            value_keys,
            entity_id=profile_id,
        )

        placement = (
            place_location(location, transform=transform, dem=dem)
            if location
            else Placement()
        )
        # Rain without a location covers the whole domain (DOCUMENTATION.md
        # 6.2); inflow / water level without one is a silent no-op in
        # CAFLOOD (6.3, 6.4) - so fail loudly instead.
        if kind != "rain" and placement.zone is None and placement.area is None:
            raise InputResolutionError(
                f"{profile_id}: {profile_type} selected without a 'location' - "
                "CAFLOOD would silently ignore it"
            )
        if kind == "waterLevel":
            if placement.cell is None:
                raise InputResolutionError(
                    f"{profile_id}: WaterLevelProfile needs a Point location "
                    "(its depths are added to the ground elevation of one DEM cell)"
                )
            ground = dem.elevation_at(*placement.cell)
            # Rounded to the mm: DEM values are float32 noise past that.
            values = [round(ground + depth, 3) for depth in values]

        events.append(
            ForcingEvent(
                kind=kind,
                name=str(name),
                times_s=times,
                values=values,
                zone=placement.zone,
                area=placement.area,
            )
        )
    return events


def collect_absolute_forcings(
    raw_input: dict[str, Any],
    stellio: StellioClient,
    *,
    timeframe: Timeframe,
    transform: CoordinateTransformer,
    dem: DemGrid,
) -> list[ForcingEvent]:
    # TODO: temporal queries on the selected ControlPoints (inflow /
    # waterLevel) and WeatherInformation (precipitation) over
    # [timeframe.start, timeframe.end], converted to seconds since
    # timeframe.start.
    raise InputResolutionError("timeframeControl='absolute' is not implemented yet")



# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def as_entity_id(item: Any) -> str:
    """Forcing entries are Relationships (resolved to {"entityId", ...} by
    resolve_input); plain entity id strings are accepted too."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and "entityId" in item:
        return item["entityId"]
    raise InputResolutionError(
        f"Could not interpret forcing entry as an entity id: {item!r}"
    )


def location_lonlat(entity: dict[str, Any]) -> tuple[float, float]:
    location = entity.get("location")
    if not location:
        raise InputResolutionError(
            f"Entity {entity.get('id')} has no 'location' GeoProperty"
        )
    coords = location["value"]["coordinates"]
    return float(coords[0]), float(coords[1])

