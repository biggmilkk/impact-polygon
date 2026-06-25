from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import geopandas as gpd
import numpy as np
import osmnx as ox
from shapely.geometry import LineString, MultiLineString, Point, Polygon, GeometryCollection, mapping, shape
from shapely.ops import linemerge, substring, unary_union

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
    output_length_m: float
    road_aligned_segments_count: int
    skipped_transition_count: int
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
    lines = lines[lines.geometry.geom_type == "LineString"].copy().reset_index(drop=True)
    return lines


def _densify_line(line: LineString, spacing_m: float) -> list[Point]:
    """Sample points along a projected LineString at approximately spacing_m intervals."""
    if line.length == 0:
        return []

    distances = list(np.arange(0, line.length, spacing_m))
    if not distances or distances[-1] != line.length:
        distances.append(line.length)

    return [line.interpolate(distance) for distance in distances]


def _choose_nearest_line_candidate(
    point: Point,
    lines: gpd.GeoDataFrame,
    previous_line_id: int | None,
    candidate_count: int,
    switch_penalty_m: float,
) -> tuple[int, LineString, Point, float, float]:
    """
    Pick a nearby line for this point.

    This is deliberately not pure nearest-line snapping. A small switch penalty prevents
    the output from jumping back and forth between parallel roads every few meters.
    """
    distances = lines.geometry.distance(point)
    nearest = distances.nsmallest(max(1, candidate_count))

    best_score = float("inf")
    best_id = int(nearest.index[0])
    best_distance = float(nearest.iloc[0])

    for line_id_raw, distance_raw in nearest.items():
        line_id = int(line_id_raw)
        distance_m = float(distance_raw)
        switch_cost = 0.0 if previous_line_id is None or line_id == previous_line_id else switch_penalty_m
        score = distance_m + switch_cost
        if score < best_score:
            best_score = score
            best_id = line_id
            best_distance = distance_m

    line = lines.loc[best_id].geometry
    distance_along_line = float(line.project(point))
    snapped_point = line.interpolate(distance_along_line)

    return best_id, line, snapped_point, best_distance, distance_along_line


def _safe_substring(line: LineString, start_m: float, end_m: float) -> LineString | None:
    """Return the part of a road/rail line between two projected positions."""
    if abs(start_m - end_m) < 0.25:
        return None

    try:
        part = substring(line, start_m, end_m, normalized=False)
    except Exception:
        lo, hi = sorted([start_m, end_m])
        part = substring(line, lo, hi, normalized=False)

    if part.is_empty or part.geom_type != "LineString" or part.length < 0.25:
        return None

    return part


def _linework_only(geometry: Any) -> LineString | MultiLineString:
    """Extract linework from LineString/MultiLineString/GeometryCollection."""
    if geometry.geom_type == "LineString":
        return geometry
    if geometry.geom_type == "MultiLineString":
        return geometry
    if geometry.geom_type == "GeometryCollection":
        lines = [g for g in geometry.geoms if g.geom_type == "LineString" and not g.is_empty]
        if not lines:
            raise ValueError("No usable snapped road/rail linework was produced.")
        return MultiLineString(lines)
    raise ValueError(f"Unsupported snapped geometry type: {geometry.geom_type}")


def snap_polygon_to_nearest_network(
    drawn_geojson: dict[str, Any],
    target: SnapTarget = "roads_and_rails",
    search_buffer_m: float = 150,
    sample_spacing_m: float = 15,
    max_snap_distance_m: float | None = 120,
    candidate_count: int = 5,
    switch_penalty_m: float = 35,
) -> SnapResult:
    """
    Snap a user-drawn polygon boundary to nearby road/rail linework.

    V2 behavior:
    - Samples the drawn polygon boundary.
    - Chooses nearby roads/rails with a small continuity penalty to avoid zig-zags.
    - Outputs actual road/rail line segments, not straight lines between snapped points.

    This prevents the ugly diagonal red lines caused by connecting independently snapped
    points with straight segments.
    """
    polygon = _ensure_polygon_from_geojson(drawn_geojson)

    query_polygon_wgs84, _ = _buffer_polygon_meters(polygon, buffer_m=search_buffer_m)

    tags = _tags_for_target(target)
    features = ox.features_from_polygon(query_polygon_wgs84, tags=tags)
    lines_wgs84 = _explode_to_lines(features)

    if lines_wgs84.empty:
        raise ValueError(
            "No road/rail line features were found near this polygon. Try increasing the search buffer."
        )

    _, polygon_projected = _project_polygon(polygon)
    metric_crs = polygon_projected.crs
    lines_projected = lines_wgs84.to_crs(metric_crs).reset_index(drop=True)

    boundary_projected = polygon_projected.geometry.iloc[0].boundary
    sampled_points = _densify_line(boundary_projected, spacing_m=sample_spacing_m)

    if len(sampled_points) < 4:
        raise ValueError(
            "The polygon is too small to sample. Try lowering sample spacing or drawing a larger polygon."
        )

    selections: list[dict[str, Any]] = []
    snap_distances: list[float] = []
    previous_line_id: int | None = None
    rejected_count = 0

    for point in sampled_points:
        line_id, line, snapped_point, distance_m, distance_along_line = _choose_nearest_line_candidate(
            point=point,
            lines=lines_projected,
            previous_line_id=previous_line_id,
            candidate_count=candidate_count,
            switch_penalty_m=switch_penalty_m,
        )

        if max_snap_distance_m is not None and distance_m > max_snap_distance_m:
            selections.append(
                {
                    "line_id": None,
                    "line": None,
                    "snapped_point": None,
                    "distance_m": distance_m,
                    "distance_along_line": None,
                }
            )
            rejected_count += 1
        else:
            selections.append(
                {
                    "line_id": line_id,
                    "line": line,
                    "snapped_point": snapped_point,
                    "distance_m": distance_m,
                    "distance_along_line": distance_along_line,
                }
            )
            previous_line_id = line_id

        snap_distances.append(distance_m)

    road_parts: list[LineString] = []
    skipped_transition_count = 0

    # Build output from the actual selected road/rail geometry.
    # Consecutive points on the same OSM line become substrings of that OSM line.
    # Consecutive points on different OSM lines are NOT connected with fake diagonals.
    for current, nxt in zip(selections[:-1], selections[1:]):
        if current["line_id"] is None or nxt["line_id"] is None:
            skipped_transition_count += 1
            continue

        if current["line_id"] != nxt["line_id"]:
            skipped_transition_count += 1
            continue

        part = _safe_substring(
            line=current["line"],
            start_m=current["distance_along_line"],
            end_m=nxt["distance_along_line"],
        )
        if part is not None:
            road_parts.append(part)

    if not road_parts:
        raise ValueError(
            "No continuous road/rail segments were produced. Try increasing max snap distance, increasing switch penalty, "
            "or drawing closer to roads."
        )

    # Dissolve overlapping snippets into cleaner linework.
    dissolved = unary_union(road_parts)
    try:
        merged = linemerge(dissolved)
    except Exception:
        merged = dissolved

    snapped_projected = _linework_only(merged)
    snapped_wgs84 = gpd.GeoDataFrame(geometry=[snapped_projected], crs=metric_crs).to_crs(
        "EPSG:4326"
    ).geometry.iloc[0]

    warning_parts = []
    if rejected_count:
        warning_parts.append(
            f"{rejected_count} sampled point(s) exceeded the max snap distance and were ignored."
        )
    if skipped_transition_count:
        warning_parts.append(
            f"{skipped_transition_count} transition(s) were not connected because doing so would create fake diagonal lines."
        )
    if snapped_projected.geom_type == "MultiLineString":
        warning_parts.append(
            "Output is a MultiLineString because the nearest roads/rails did not form one continuous boundary."
        )

    return SnapResult(
        snapped_line_geojson=mapping(snapped_wgs84),
        original_polygon_geojson=mapping(polygon),
        query_area_geojson=mapping(query_polygon_wgs84),
        network_features_count=int(len(lines_wgs84)),
        sampled_points_count=int(len(sampled_points)),
        mean_snap_distance_m=float(np.mean(snap_distances)),
        max_snap_distance_m=float(np.max(snap_distances)),
        output_length_m=float(snapped_projected.length),
        road_aligned_segments_count=int(len(road_parts)),
        skipped_transition_count=int(skipped_transition_count),
        warning=" ".join(warning_parts) if warning_parts else None,
    )
