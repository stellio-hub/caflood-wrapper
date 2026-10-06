"""caflood_inputs.py: payload, input resolution, timeframe, DEM, placement, profiles."""

from __future__ import annotations

from datetime import timedelta

import pytest

from caflood_common import InputResolutionError
from caflood_inputs import (
    CoordinateTransformer,
    DemGrid,
    collect_relative_forcings,
    entity_type,
    parse_iso_duration,
    parse_payload,
    parse_timeframe,
    place_location,
    resolve_input,
)
from conftest import (
    DEM_CELL,
    DEM_CRS,
    PROFILES,
    FakeStellio,
    cell_center,
    dem_elevation,
    relationship,
    relative_execution,
)


# --- payload / resolution ----------------------------------------------------

def test_parse_payload_envelope():
    payload = parse_payload({"id": "urn:x:1", "entityId": "urn:y:2", "input": {"a": 1}})
    assert (payload.execution_id, payload.entity_id, payload.input) == ("urn:x:1", "urn:y:2", {"a": 1})


def test_parse_payload_flat_notification():
    raw = {"serviceExecutionId": {"type": "Property", "value": "urn:ngsi-ld:ServiceExecution:abc"},
           "studyAreaId": {"type": "Property", "value": "urn:ngsi-ld:StudyArea:s1"},
           "duration": {"type": "Property", "value": "PT1H"}}
    payload = parse_payload(raw)
    assert (payload.execution_id, payload.entity_id) == ("urn:ngsi-ld:ServiceExecution:abc", "urn:ngsi-ld:StudyArea:s1")
    assert payload.input is raw  # the notification IS the input
    # plain strings work too, and a bare UUID gets the ServiceExecution prefix
    plain = parse_payload({"serviceExecutionId": "abc", "studyAreaId": "urn:ngsi-ld:StudyArea:s1"})
    assert plain.execution_id == "urn:ngsi-ld:ServiceExecution:abc"


@pytest.mark.parametrize("bad, missing", [
    ({"input": {}}, "id"), ({"id": "a", "input": {}}, "entityId"),
    ({"studyAreaId": "urn:s"}, "serviceExecutionId"), ({"serviceExecutionId": "urn:e"}, "studyAreaId"),
    ({"serviceExecutionId": {"type": "Property", "value": ""}, "studyAreaId": "urn:s"}, "serviceExecutionId"),
    ([], None),
])
def test_parse_payload_rejects(bad, missing):
    with pytest.raises(InputResolutionError, match=missing or "JSON object"):
        parse_payload(bad)


def test_resolve_input_value_types():
    stellio = FakeStellio({"urn:e:1": {"id": "urn:e:1", "type": "T", "height": {"type": "Property", "value": 12}}})
    assert resolve_input({"type": "Property", "value": 3}, stellio) == 3
    assert resolve_input({"type": "JsonProperty", "json": {"a": [1]}}, stellio) == {"a": [1]}
    assert resolve_input({"type": "GeoProperty", "value": {"type": "Point"}}, stellio) == {"type": "Point"}
    assert resolve_input({"type": "EntityAttributeSelector", "entityId": "urn:e:1", "attributeName": "height"}, stellio) == 12
    assert resolve_input("plain", stellio) == "plain"
    rel = resolve_input({"type": "Relationship", "object": "urn:p:1",
                         "location": {"type": "GeoProperty", "value": {"type": "Point", "coordinates": [1, 2]}}}, stellio)
    assert rel == {"entityId": "urn:p:1", "location": {"type": "Point", "coordinates": [1, 2]}}
    with pytest.raises(InputResolutionError):
        resolve_input({"type": "Unknown"}, stellio)


def test_entity_type_handles_expanded_iri():
    assert entity_type({"type": "InflowProfile"}) == "InflowProfile"
    assert entity_type({"type": "https://uri.etsi.org/ngsi-ld/default-context/InflowProfile"}) == "InflowProfile"


# --- timeframe ---------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("PT36H", timedelta(hours=36)), ("P3D", timedelta(days=3)), ("P1DT12H", timedelta(days=1, hours=12)),
    ("P2W", timedelta(weeks=2)), ("PT1.5H", timedelta(minutes=90)), ("PT90S", timedelta(seconds=90)),
])
def test_parse_iso_duration(text, expected):
    assert parse_iso_duration(text) == expected


@pytest.mark.parametrize("text", ["P1Y", "P1M", "P", "PT", "3h", ""])
def test_parse_iso_duration_rejects(text):
    with pytest.raises(InputResolutionError):
        parse_iso_duration(text)


def test_parse_timeframe_relative_and_absolute():
    stellio = FakeStellio({})
    rel = parse_timeframe({"timeframeControl": {"type": "Property", "value": "relative"},
                           "duration": {"type": "Property", "value": "PT2H"}}, stellio)
    assert (rel.mode, rel.duration_s) == ("relative", 7200)
    absolute = parse_timeframe({"timeframeControl": {"type": "Property", "value": "absolute"},
                                "timeStart": {"type": "Property", "value": "2026-07-03T22:00:00.000Z"},
                                "timeEnd": {"type": "Property", "value": "2026-07-04T22:00:00.000Z"}}, stellio)
    assert (absolute.mode, absolute.duration_s) == ("absolute", 86400)
    with pytest.raises(InputResolutionError):
        parse_timeframe({"timeframeControl": {"type": "Property", "value": "relative"}}, stellio)
    with pytest.raises(InputResolutionError):
        parse_timeframe({"timeframeControl": {"type": "Property", "value": "sometimes"}}, stellio)


# --- DEM + placement ---------------------------------------------------------

def test_dem_header_and_elevation(dem_path):
    dem = DemGrid.from_asc(dem_path)
    assert (dem.ncols, dem.nrows, dem.cellsize) == (20, 10, DEM_CELL)
    assert dem.cell_of(*cell_center(5, 4)) == (5, 4)
    assert dem.elevation_at(5, 4) == dem_elevation(5, 4)
    assert dem.elevation_at(9, 19) == dem_elevation(9, 19)
    with pytest.raises(InputResolutionError, match="NODATA"):
        dem.elevation_at(0, 0)
    with pytest.raises(InputResolutionError, match="outside the DEM"):
        dem.cell_of(0.0, 0.0)


def test_place_point_targets_exactly_one_cell(dem_path, lonlat_of_cell):
    dem = DemGrid.from_asc(dem_path)
    transform = CoordinateTransformer(DEM_CRS)
    placement = place_location({"type": "Point", "coordinates": lonlat_of_cell(5, 4)}, transform=transform, dem=dem)
    cx, cy = cell_center(5, 4)
    # CAFLOOD: zone = cell containing (tlx, tly) + ceil(w/cellsize) x ceil(h/cellsize) cells.
    assert placement.cell == (5, 4)
    assert placement.zone == pytest.approx((cx, cy, DEM_CELL, DEM_CELL))
    assert placement.area is None


def test_place_polygon_gives_area_bbox(dem_path, lonlat_of_cell):
    dem = DemGrid.from_asc(dem_path)
    ring = [lonlat_of_cell(1, 1), lonlat_of_cell(1, 5), lonlat_of_cell(4, 5), lonlat_of_cell(4, 1), lonlat_of_cell(1, 1)]
    placement = place_location({"type": "Polygon", "coordinates": [ring]}, transform=CoordinateTransformer(DEM_CRS), dem=dem)
    tlx, tly, brx, bry = placement.area
    assert (tlx, tly) == pytest.approx(cell_center(1, 1), abs=0.01)
    assert (brx, bry) == pytest.approx(cell_center(4, 5), abs=0.01)
    assert placement.zone is None


def test_unknown_crs_is_an_input_error():
    with pytest.raises(InputResolutionError):
        CoordinateTransformer("EPSG:999999")


# --- profiles -> forcing events ----------------------------------------------

def _forcings(dem_path, execution):
    return collect_relative_forcings(execution["input"], FakeStellio(PROFILES),
                                     transform=CoordinateTransformer(DEM_CRS), dem=DemGrid.from_asc(dem_path))


def test_profiles_dispatch_on_entity_type(dem_path, lonlat_of_cell):
    # The rain profile is deliberately under hydraulicForcing: the field must not matter.
    execution = relative_execution(
        hydraulic=[relationship("urn:ngsi-ld:InflowProfile:pulse", lonlat_of_cell(5, 4)),
                   relationship("urn:ngsi-ld:RainfallProfile:burst")],
        rainfall=[relationship("urn:ngsi-ld:WaterLevelProfile:tail", lonlat_of_cell(2, 15))],
    )
    inflow, rain, level = _forcings(dem_path, execution)

    assert (inflow.kind, inflow.name) == ("inflow", "Short pulse inflow")
    assert inflow.times_s == [0, 60, 180, 240] and inflow.values == [0, 80, 80, 0]  # "time" key accepted
    assert inflow.zone == pytest.approx((*cell_center(5, 4), DEM_CELL, DEM_CELL))

    assert rain.kind == "rain" and rain.zone is None and rain.area is None  # whole domain

    # Water level profile = depth above ground: ground elevation of the cell is added.
    ground = dem_elevation(2, 15)
    assert level.kind == "waterLevel"
    assert level.values == [ground + 0, ground + 30, ground + 30, ground + 0]


def test_same_profile_twice_is_kept(dem_path, lonlat_of_cell):
    execution = relative_execution(
        hydraulic=[relationship("urn:ngsi-ld:InflowProfile:pulse", lonlat_of_cell(5, 4)),
                   relationship("urn:ngsi-ld:InflowProfile:pulse", lonlat_of_cell(6, 8))],
        rainfall=[],
    )
    events = _forcings(dem_path, execution)
    assert [e.zone[:2] for e in events] == [pytest.approx(cell_center(5, 4)), pytest.approx(cell_center(6, 8))]


@pytest.mark.parametrize("profile_id", ["urn:ngsi-ld:InflowProfile:pulse", "urn:ngsi-ld:WaterLevelProfile:tail"])
def test_hydraulic_forcing_without_location_fails(dem_path, profile_id):
    with pytest.raises(InputResolutionError, match="without a 'location'"):
        _forcings(dem_path, relative_execution(hydraulic=[relationship(profile_id)], rainfall=[]))


def test_water_level_on_nodata_cell_fails(dem_path, lonlat_of_cell):
    execution = relative_execution(hydraulic=[relationship("urn:ngsi-ld:WaterLevelProfile:tail", lonlat_of_cell(0, 0))],
                                   rainfall=[])
    with pytest.raises(InputResolutionError, match="NODATA"):
        _forcings(dem_path, execution)


def test_unsupported_profile_type_fails(dem_path):
    stellio = FakeStellio({"urn:x:ControlPoint:1": {"id": "urn:x:ControlPoint:1", "type": "ControlPoint"}})
    execution = relative_execution(hydraulic=[relationship("urn:x:ControlPoint:1")], rainfall=[])
    with pytest.raises(InputResolutionError, match="unsupported forcing entity type"):
        collect_relative_forcings(execution["input"], stellio, transform=CoordinateTransformer(DEM_CRS),
                                  dem=DemGrid.from_asc(dem_path))
