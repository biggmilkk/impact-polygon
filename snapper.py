from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

import geopandas as gpd
import numpy as np
import osmnx as ox
from shapely.geometry import LineString, MultiLineString, Point, Polygon, mapping, shape
from shapely.ops import unary_union

SnapTarget = Literal["roads", "rails", "roads_and_rails"]


@dataclass
class SnapResult:
    snapped_line_geojson: dict[str, Any]
    original_polygon_geojson: dict[str, Any]
    query_area_geojson: dict[str, Any]
    network_features_count: int
    sampled_points_count: int
    mean_snap_distance_m: float
    max_snap_distance_m: float
    warning: str | None = None


def _ensure_polygon_from_geojson(geojson: dict[str, Any]) -> Polygon:
    """Return a valid Polygon from a GeoJSON Feature or Geometry."""
    geometry = geojson.get("geometry", geojson)
    geom = shape(geometry)

    if geom.geom_type != "Polygon":
        raise ValueError("Please draw a Polygon, not a LineString, Point, or Rectangle layer object.")

    if not geom.is_valid:
        geom = geom.buffer(0)

    if geom.is_empty or geom.geom_type != "Polygon":
        raise ValueError("The polygon is invalid or empty. Try drawing a simpler polygon.")

    return geom


def _project_polygon(polygon: Polygon) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return polygon in WGS84 and projected metric CRS."""
    polygon_wgs84 = gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:4326")
    polygon_projected = ox.projection.project_gdf(polygon_wgs84)
    return polygon_wgs84, polygon_projected


def _buffer_polygon_meters(polygon: Polygon, buffer_m: float) -> tuple[Polygon, gpd.GeoDataFrame]:
    """Create a WGS84 buffer polygon using a local projected CRS."""
    _, projected = _project_polygon(polygon)
    buffered_projected = projected.copy()
    buffered_projected["geometry"] = buffered_projected.geometry.buffer(buffer_m)
    buffered_wgs84 = buffered_projected.to_crs("EPSG:4326")
    return buffered_wgs84.geometry.iloc[0], buffered_projected


def _tags_for_target(target: SnapTarget) -> dict[str, Any]:
    road_tags = {"highway": True}
    rail_tags = {"railway": ["rail", "light_rail", "subway", "tram", "narrow_gauge"]}

    if target == "roads":
        return road_tags
    if target == "rails":
        return rail_tags
    return {**road_tags, **rail_tags}


def _explode_to_lines(features: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Keep only LineString/MultiLineString features and explode multipart lines."""
    if features.empty:
        return features

    lines = features[features.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
    if lines.empty:
        return lines

    lines = lines.explode(index_parts=False).reset_index(drop=True)
    lines = lines[lines.geometry.geom_type == "LineString"].copy()
    return lines


def _densify_line(line: LineString, spacing_m: float) -> list[Point]:
    """Sample points along a projected LineString at approximately spacing_m intervals."""
    if line.length == 0:
        return []

    distances = list(np.arange(0, line.length, spacing_m))
    if not distances or distances[-1] != line.length:
        distances.append(line.length)

    return [line.interpolate(distance) for distance in distances]


def _nearest_point_on_lines(point: Point, lines: gpd.GeoDataFrame) -> tuple[Point, float]:
    """
    Find the nearest line geometry and project the point onto it.

    This deliberately uses a simple distance scan for MVP reliability on Streamlit Cloud.
    For larger datasets, replace this with GeoPandas sindex.nearest or a Shapely STRtree.
    """
    distances = lines.geometry.distance(point)
    nearest_idx = distances.idxmin()
    nearest_line = lines.loc[nearest_idx].geometry
    snap_distance = float(distances.loc[nearest_idx])
    snapped_point = nearest_line.interpolate(nearest_line.project(point))
    return snapped_point, snap_distance


def snap_polygon_to_nearest_network(
    drawn_geojson: dict[str, Any],
    target: SnapTarget = "roads_and_rails",
    search_buffer_m: float = 150,
    sample_spacing_m: float = 25,
    max_snap_distance_m: float | None = 120,
) -> SnapResult:
    """
    Snap a user-drawn polygon boundary to the nearest road/rail geometries.

    The result is a closed LineString that may shift inward or outward based on the
    closest OSM road/rail feature at each sampled boundary point.
    """
    polygon = _ensure_polygon_from_geojson(drawn_geojson)

    query_polygon_wgs84, query_polygon_projected = _buffer_polygon_meters(
        polygon, buffer_m=search_buffer_m
    )

    tags = _tags_for_target(target)
    features = ox.features_from_polygon(query_polygon_wgs84, tags=tags)
    lines_wgs84 = _explode_to_lines(features)

    if lines_wgs84.empty:
        raise ValueError(
            "No road/rail line features were found near this polygon. Try increasing the search buffer."
        )

    _, polygon_projected = _project_polygon(polygon)
    metric_crs = polygon_projected.crs
    lines_projected = lines_wgs84.to_crs(metric_crs)

    boundary_projected = polygon_projected.geometry.iloc[0].boundary
    sampled_points = _densify_line(boundary_projected, spacing_m=sample_spacing_m)

    if len(sampled_points) < 4:
        raise ValueError("The polygon is too small to sample. Try lowering sample spacing or drawing a larger polygon.")

    snapped_points: list[Point] = []
    snap_distances: list[float] = []
    rejected_count = 0

    for point in sampled_points:
        snapped_point, distance_m = _nearest_point_on_lines(point, lines_projected)

        if max_snap_distance_m is not None and distance_m > max_snap_distance_m:
            # Keep the original sampled point instead of snapping unrealistically far away.
            snapped_points.append(point)
            rejected_count += 1
        else:
            snapped_points.append(snapped_point)

        snap_distances.append(distance_m)

    # Ensure closed boundary line.
    if not snapped_points[0].equals(snapped_points[-1]):
        snapped_points.append(snapped_points[0])

    snapped_line_projected = LineString(snapped_points)
    snapped_line_wgs84 = gpd.GeoDataFrame(geometry=[snapped_line_projected], crs=metric_crs).to_crs(
        "EPSG:4326"
    ).geometry.iloc[0]

    warning = None
    if rejected_count:
        warning = (
            f"{rejected_count} sampled point(s) exceeded the max snap distance and were left unsnapped. "
            "Increase max_snap_distance_m if needed."
        )

    return SnapResult(
        snapped_line_geojson=mapping(snapped_line_wgs84),
        original_polygon_geojson=mapping(polygon),
        query_area_geojson=mapping(query_polygon_wgs84),
        network_features_count=int(len(lines_wgs84)),
        sampled_points_count=int(len(sampled_points)),
        mean_snap_distance_m=float(np.mean(snap_distances)),
        max_snap_distance_m=float(np.max(snap_distances)),
        warning=warning,
    )
