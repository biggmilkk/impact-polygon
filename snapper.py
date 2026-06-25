from __future__ import annotations

from dataclasses import dataclass
from math import log, sqrt
from typing import Any, Literal

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
from shapely.geometry import LineString, MultiLineString, Polygon, mapping, shape
from shapely.ops import polygonize, unary_union

SnapTarget = Literal["roads", "rails", "roads_and_rails"]
RoadTier = Literal["arterial", "main", "public", "all_drivable"]
FitMode = Literal["balanced", "tight", "cover"]

# Deliberately exclude footway/path/pedestrian/crossing/cycleway/track/steps.
# This app is drawing a road/rail polygon boundary, not a human route.
ROAD_TIERS: dict[RoadTier, set[str]] = {
    # Coarsest option. Good when small side streets make noisy boundaries.
    "arterial": {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
    },
    # Recommended for large urban polygons. Includes tertiary/collector roads.
    "main": {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
    },
    # Use when you need smaller public streets as valid polygon boundaries.
    "public": {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "living_street",
        "road",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
    },
    # Usually too noisy for clean polygons, but useful for sites/campuses.
    "all_drivable": {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "living_street",
        "road",
        "service",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
    },
}

RAIL_VALUES = {"rail", "light_rail", "subway", "tram", "narrow_gauge", "monorail"}


@dataclass
class SnapResult:
    snapped_geojson: dict[str, Any]
    snapped_boundary_geojson: dict[str, Any]
    original_polygon_geojson: dict[str, Any]
    query_area_geojson: dict[str, Any]
    algorithm: str
    network_nodes_count: int
    network_edges_count: int
    candidate_cells_count: int
    initially_selected_cells_count: int
    final_selected_cells_count: int
    output_area_m2: float
    output_perimeter_m: float
    coordinate_count: int
    closed_loop: bool
    fit_score: float
    coverage_ratio: float
    outside_ratio: float
    missing_ratio: float
    warning: str | None = None


def _ensure_polygon_from_geojson(geojson: dict[str, Any]) -> Polygon:
    geometry = geojson.get("geometry", geojson)
    geom = shape(geometry)

    if geom.geom_type != "Polygon":
        raise ValueError("Please draw one Polygon. Rectangles are fine if the drawing tool returns them as Polygon GeoJSON.")

    if not geom.is_valid:
        geom = geom.buffer(0)

    if geom.is_empty or geom.geom_type != "Polygon":
        raise ValueError("The polygon is invalid or empty. Try drawing a simpler polygon.")

    return geom


def _polygon_gdf(polygon: Polygon) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:4326")


def _buffer_polygon_meters(polygon: Polygon, buffer_m: float) -> Polygon:
    polygon_wgs84 = _polygon_gdf(polygon)
    polygon_projected = ox.projection.project_gdf(polygon_wgs84)
    buffered_projected = polygon_projected.geometry.iloc[0].buffer(float(buffer_m))
    buffered_wgs84 = gpd.GeoDataFrame(geometry=[buffered_projected], crs=polygon_projected.crs).to_crs("EPSG:4326")
    return buffered_wgs84.geometry.iloc[0]


def _custom_filter_for_target(target: SnapTarget, road_tier: RoadTier) -> str | list[str]:
    road_values = "|".join(sorted(ROAD_TIERS[road_tier]))
    rail_values = "|".join(sorted(RAIL_VALUES))

    road_filter = f'["highway"~"{road_values}"]'
    rail_filter = f'["railway"~"{rail_values}"]'

    if target == "roads":
        return road_filter
    if target == "rails":
        return rail_filter
    return [road_filter, rail_filter]


def _tag_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value if item is not None}
    return {str(value)}


def _edge_is_allowed(attrs: dict[str, Any], target: SnapTarget, road_tier: RoadTier) -> bool:
    highway_values = _tag_values(attrs.get("highway"))
    railway_values = _tag_values(attrs.get("railway"))

    is_allowed_road = bool(highway_values & ROAD_TIERS[road_tier])
    is_allowed_rail = bool(railway_values & RAIL_VALUES)

    if target == "roads":
        return is_allowed_road
    if target == "rails":
        return is_allowed_rail
    return is_allowed_road or is_allowed_rail


def _to_undirected_graph(graph: nx.MultiDiGraph | nx.MultiGraph) -> nx.MultiGraph:
    try:
        return ox.convert.to_undirected(graph)
    except Exception:
        return graph.to_undirected()


def _filter_graph_edges(
    graph: nx.MultiDiGraph | nx.MultiGraph,
    target: SnapTarget,
    road_tier: RoadTier,
) -> nx.MultiDiGraph | nx.MultiGraph:
    filtered = graph.copy()

    if isinstance(filtered, (nx.MultiGraph, nx.MultiDiGraph)):
        to_remove = [
            (u, v, k)
            for u, v, k, attrs in filtered.edges(keys=True, data=True)
            if not _edge_is_allowed(attrs, target=target, road_tier=road_tier)
        ]
        filtered.remove_edges_from(to_remove)
    else:
        to_remove = [
            (u, v)
            for u, v, attrs in filtered.edges(data=True)
            if not _edge_is_allowed(attrs, target=target, road_tier=road_tier)
        ]
        filtered.remove_edges_from(to_remove)

    filtered.remove_nodes_from(list(nx.isolates(filtered)))
    return filtered


def _prune_dead_ends(graph: nx.MultiGraph) -> nx.MultiGraph:
    """Remove dangling branches. Polygon boundaries should come from cyclic linework only."""
    pruned = graph.copy()

    while True:
        leaves = [node for node, degree in pruned.degree() if degree <= 1]
        if not leaves:
            break
        pruned.remove_nodes_from(leaves)

    if pruned.number_of_edges() == 0:
        return graph
    return pruned


def _line_geometries_from_edges(edges: gpd.GeoDataFrame) -> list[LineString]:
    lines: list[LineString] = []
    for geom in edges.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "LineString":
            if geom.length > 0:
                lines.append(geom)
        elif geom.geom_type == "MultiLineString":
            lines.extend([part for part in geom.geoms if not part.is_empty and part.length > 0])
    return lines


def _count_coordinates(geom: Any) -> int:
    if geom.is_empty:
        return 0
    if geom.geom_type in {"Point"}:
        return 1
    if geom.geom_type in {"LineString", "LinearRing"}:
        return len(geom.coords)
    if geom.geom_type == "Polygon":
        return len(geom.exterior.coords) + sum(len(ring.coords) for ring in geom.interiors)
    if hasattr(geom, "geoms"):
        return sum(_count_coordinates(part) for part in geom.geoms)
    return 0


def _sample_line(line: Any, sample_count: int) -> list[Any]:
    if line.is_empty or line.length <= 0:
        return []
    if sample_count <= 1:
        return [line.interpolate(0.5, normalized=True)]
    return [line.interpolate(float(i) / float(sample_count - 1), normalized=True) for i in range(sample_count)]


def _mean_boundary_distance(candidate: Any, drawn_polygon: Polygon, sample_count: int = 60) -> float:
    """A cheap symmetric average distance between output boundary and input boundary."""
    if candidate.is_empty:
        return 1e9
    cand_boundary = candidate.boundary
    drawn_boundary = drawn_polygon.boundary
    cand_points = _sample_line(cand_boundary, sample_count)
    drawn_points = _sample_line(drawn_boundary, sample_count)

    distances: list[float] = []
    distances.extend(point.distance(drawn_boundary) for point in cand_points)
    distances.extend(point.distance(cand_boundary) for point in drawn_points)
    if not distances:
        return 1e9
    return float(np.mean(distances))


def _fit_weights(mode: FitMode) -> dict[str, float]:
    if mode == "tight":
        # Prefer staying inside/near the drawn polygon. Good for avoiding big outside bulges.
        return {"coverage": 2.2, "outside": 2.8, "missing": 0.9, "area": 0.35, "boundary": 0.75, "simplicity": 0.015}
    if mode == "cover":
        # Prefer covering the user's polygon, even if the snapped result expands outward.
        return {"coverage": 2.8, "outside": 1.0, "missing": 2.2, "area": 0.30, "boundary": 0.65, "simplicity": 0.010}
    # Balanced default: good first choice for inward/outward snapping.
    return {"coverage": 2.5, "outside": 1.7, "missing": 1.5, "area": 0.35, "boundary": 0.70, "simplicity": 0.012}


def _polygon_parts(geom: Any) -> list[Polygon]:
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [part for part in geom.geoms if not part.is_empty and part.area > 0]
    return []


def _score_polygon(candidate: Any, drawn_polygon: Polygon, mode: FitMode) -> dict[str, float]:
    if candidate.is_empty:
        return {
            "score": -1e9,
            "coverage_ratio": 0.0,
            "outside_ratio": 1.0,
            "missing_ratio": 1.0,
            "boundary_distance_m": 1e9,
        }

    candidate_area = max(float(candidate.area), 1.0)
    drawn_area = max(float(drawn_polygon.area), 1.0)
    intersection_area = float(candidate.intersection(drawn_polygon).area)

    coverage_ratio = intersection_area / drawn_area
    outside_ratio = max(candidate_area - intersection_area, 0.0) / candidate_area
    missing_ratio = max(drawn_area - intersection_area, 0.0) / drawn_area
    area_ratio = candidate_area / drawn_area
    area_penalty = abs(log(max(area_ratio, 1e-9)))

    boundary_distance_m = _mean_boundary_distance(candidate, drawn_polygon, sample_count=50)
    distance_scale = max(sqrt(drawn_area), 1.0)
    boundary_distance_norm = boundary_distance_m / distance_scale

    coord_count = _count_coordinates(candidate)
    simplicity_penalty = coord_count / 1000.0

    weights = _fit_weights(mode)
    score = (
        weights["coverage"] * coverage_ratio
        - weights["outside"] * outside_ratio
        - weights["missing"] * missing_ratio
        - weights["area"] * area_penalty
        - weights["boundary"] * boundary_distance_norm
        - weights["simplicity"] * simplicity_penalty
    )

    return {
        "score": float(score),
        "coverage_ratio": float(coverage_ratio),
        "outside_ratio": float(outside_ratio),
        "missing_ratio": float(missing_ratio),
        "boundary_distance_m": float(boundary_distance_m),
    }


def _best_single_component(geom: Any, drawn_polygon: Polygon, mode: FitMode) -> tuple[Any, bool, dict[str, float]]:
    parts = _polygon_parts(geom)
    if not parts:
        return geom, False, _score_polygon(geom, drawn_polygon, mode)
    if len(parts) == 1:
        return parts[0], False, _score_polygon(parts[0], drawn_polygon, mode)

    scored = [(part, _score_polygon(part, drawn_polygon, mode)) for part in parts]
    best_part, best_score = max(scored, key=lambda item: item[1]["score"])
    return best_part, True, best_score


def _safe_union(polygons: list[Polygon]) -> Any:
    if not polygons:
        return Polygon()
    geom = unary_union(polygons)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def _initial_cell_selection(
    cells: list[Polygon],
    drawn_polygon: Polygon,
    min_cell_inside_ratio: float,
    max_cell_outside_ratio: float,
) -> list[int]:
    selected: list[int] = []
    drawn_area = max(drawn_polygon.area, 1.0)

    for i, cell in enumerate(cells):
        inter_area = cell.intersection(drawn_polygon).area
        if inter_area <= 0:
            continue

        inside_ratio = inter_area / max(cell.area, 1.0)
        outside_ratio = max(cell.area - inter_area, 0.0) / max(cell.area, 1.0)
        center_inside = cell.representative_point().within(drawn_polygon)
        drawn_coverage = inter_area / drawn_area

        # The last clause catches large cells that cover a meaningful part of the drawn polygon.
        # It is deliberately stricter than V4 to avoid huge outside bulges.
        if inside_ratio >= min_cell_inside_ratio:
            selected.append(i)
        elif center_inside and outside_ratio <= max_cell_outside_ratio:
            selected.append(i)
        elif drawn_coverage >= 0.06 and outside_ratio <= max_cell_outside_ratio:
            selected.append(i)

    if selected:
        return selected

    # Fallback: pick a few cells with the best overlap with the drawn polygon.
    ranked = []
    for i, cell in enumerate(cells):
        inter_area = cell.intersection(drawn_polygon).area
        if inter_area <= 0:
            continue
        inside_ratio = inter_area / max(cell.area, 1.0)
        outside_ratio = max(cell.area - inter_area, 0.0) / max(cell.area, 1.0)
        ranked.append((inside_ratio - outside_ratio, i))

    ranked.sort(reverse=True)
    return [i for _, i in ranked[:5]]


def _local_cell_refinement(
    cells: list[Polygon],
    selected_indices: list[int],
    drawn_polygon: Polygon,
    mode: FitMode,
    max_iterations: int,
) -> tuple[list[int], Any, dict[str, float], int, int]:
    """
    Greedily remove/add whole road cells to improve shape fit.

    This is the key V5 change. V4 selected all cells meeting a local overlap rule and
    often kept the largest component. V5 scores the whole output polygon against the
    user's drawn polygon, so outside bulges and disconnected artifacts are penalized.
    """
    selected = set(selected_indices)
    remove_count = 0
    add_count = 0

    def current_geometry(indices: set[int]) -> tuple[Any, dict[str, float], bool]:
        geom = _safe_union([cells[i] for i in sorted(indices)])
        geom, reduced, score = _best_single_component(geom, drawn_polygon, mode)
        return geom, score, reduced

    current_geom, current_score, _ = current_geometry(selected)
    best_value = current_score["score"]

    for _ in range(max_iterations):
        changed = False

        # Try removing cells. This removes the red bulges/pieces that do not help fit.
        best_remove: int | None = None
        best_remove_geom = current_geom
        best_remove_score = current_score
        best_remove_value = best_value

        for idx in list(selected):
            trial = set(selected)
            trial.remove(idx)
            if not trial:
                continue
            trial_geom, trial_score, _ = current_geometry(trial)
            trial_value = trial_score["score"]
            if trial_value > best_remove_value + 1e-6:
                best_remove = idx
                best_remove_geom = trial_geom
                best_remove_score = trial_score
                best_remove_value = trial_value

        if best_remove is not None:
            selected.remove(best_remove)
            current_geom = best_remove_geom
            current_score = best_remove_score
            best_value = best_remove_value
            remove_count += 1
            changed = True

        # Try adding nearby cells that improve coverage without causing too much outside area.
        # Keep this conservative to avoid swallowing whole neighborhoods.
        if len(selected) < len(cells):
            buffered_current = current_geom.buffer(1.0)
            candidate_adds = []
            for idx, cell in enumerate(cells):
                if idx in selected:
                    continue
                if not cell.intersects(drawn_polygon) and not cell.touches(buffered_current):
                    continue
                if not cell.intersects(buffered_current):
                    continue
                candidate_adds.append(idx)

            best_add: int | None = None
            best_add_geom = current_geom
            best_add_score = current_score
            best_add_value = best_value

            for idx in candidate_adds:
                trial = set(selected)
                trial.add(idx)
                trial_geom, trial_score, _ = current_geometry(trial)
                trial_value = trial_score["score"]
                if trial_value > best_add_value + 1e-6:
                    best_add = idx
                    best_add_geom = trial_geom
                    best_add_score = trial_score
                    best_add_value = trial_value

            if best_add is not None:
                selected.add(best_add)
                current_geom = best_add_geom
                current_score = best_add_score
                best_value = best_add_value
                add_count += 1
                changed = True

        if not changed:
            break

    return sorted(selected), current_geom, current_score, remove_count, add_count


def _build_fitted_cell_polygon(
    edges_projected: gpd.GeoDataFrame,
    drawn_polygon_projected: Polygon,
    min_cell_area_m2: float,
    max_cell_area_multiple: float,
    min_cell_inside_ratio: float,
    max_cell_outside_ratio: float,
    simplify_tolerance_m: float,
    fit_mode: FitMode,
    max_refinement_iterations: int,
) -> tuple[Any, dict[str, Any]]:
    lines = _line_geometries_from_edges(edges_projected)
    if not lines:
        raise ValueError("No usable road/rail line geometry was found after filtering.")

    noded_linework = unary_union(lines)
    drawn_area = max(float(drawn_polygon_projected.area), 1.0)
    max_cell_area = drawn_area * float(max_cell_area_multiple)

    raw_cells = []
    for cell in polygonize(noded_linework):
        if cell.is_empty or cell.area < float(min_cell_area_m2):
            continue
        # Very large cells usually represent the exterior around sparse roads and cause bad bulges.
        if cell.area > max_cell_area:
            continue
        if not cell.intersects(drawn_polygon_projected):
            continue
        raw_cells.append(cell)

    if not raw_cells:
        raise ValueError(
            "No closed road cells intersect the drawn polygon. Try a larger search buffer, a lower road tier, or a lower minimum cell area."
        )

    initial_selected = _initial_cell_selection(
        cells=raw_cells,
        drawn_polygon=drawn_polygon_projected,
        min_cell_inside_ratio=min_cell_inside_ratio,
        max_cell_outside_ratio=max_cell_outside_ratio,
    )

    if not initial_selected:
        raise ValueError("Road cells were found, but none matched the drawn polygon closely enough. Try Cover mode or lower thresholds.")

    final_indices, fitted, score, removed, added = _local_cell_refinement(
        cells=raw_cells,
        selected_indices=initial_selected,
        drawn_polygon=drawn_polygon_projected,
        mode=fit_mode,
        max_iterations=max_refinement_iterations,
    )

    reduced_to_best = False
    fitted, reduced_to_best, score = _best_single_component(fitted, drawn_polygon_projected, fit_mode)

    if simplify_tolerance_m > 0:
        fitted = fitted.simplify(float(simplify_tolerance_m), preserve_topology=True)
        if not fitted.is_valid:
            fitted = fitted.buffer(0)
        fitted, reduced_again, score = _best_single_component(fitted, drawn_polygon_projected, fit_mode)
        reduced_to_best = reduced_to_best or reduced_again

    if fitted.is_empty:
        raise ValueError("The snapped polygon became empty after fitting. Try a lower simplify tolerance.")

    meta = {
        "candidate_cells_count": len(raw_cells),
        "initially_selected_cells_count": len(initial_selected),
        "final_selected_cells_count": len(final_indices),
        "fit_score": score["score"],
        "coverage_ratio": score["coverage_ratio"],
        "outside_ratio": score["outside_ratio"],
        "missing_ratio": score["missing_ratio"],
        "boundary_distance_m": score["boundary_distance_m"],
        "removed_cells_count": removed,
        "added_cells_count": added,
        "reduced_to_best": reduced_to_best,
    }
    return fitted, meta


def snap_polygon_to_road_rail_polygon(
    drawn_geojson: dict[str, Any],
    target: SnapTarget = "roads",
    road_tier: RoadTier = "main",
    search_buffer_m: float = 300,
    min_cell_area_m2: float = 500,
    max_cell_area_multiple: float = 2.0,
    min_cell_inside_ratio: float = 0.35,
    max_cell_outside_ratio: float = 0.65,
    simplify_tolerance_m: float = 12,
    prune_dead_ends: bool = True,
    fit_mode: FitMode = "balanced",
    max_refinement_iterations: int = 30,
) -> SnapResult:
    """
    Snap a drawn polygon to a best-fitting closed road/rail cell polygon.

    V5 behavior:
    - Direction is ignored: road graph is undirected.
    - Footways, pedestrian paths, crossings, cycleways, tracks, and steps are excluded.
    - Service roads are excluded unless road_tier='all_drivable'.
    - Dead-end branches are removed before polygonizing.
    - Whole closed cells are selected, then globally scored against the drawn polygon.
    - The final component is chosen by best fit, not largest area.
    - Greedy refinement removes outside bulges and adds only cells that improve fit.
    """
    polygon = _ensure_polygon_from_geojson(drawn_geojson)
    query_polygon_wgs84 = _buffer_polygon_meters(polygon, buffer_m=search_buffer_m)

    custom_filter = _custom_filter_for_target(target=target, road_tier=road_tier)
    graph = ox.graph_from_polygon(
        query_polygon_wgs84,
        network_type="all",
        custom_filter=custom_filter,
        simplify=True,
        retain_all=True,
        truncate_by_edge=True,
    )

    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        raise ValueError("No matching roads/rails were found near this polygon. Try increasing the search buffer or lowering the road tier.")

    graph = _filter_graph_edges(graph, target=target, road_tier=road_tier)
    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        raise ValueError("All downloaded network edges were filtered out. Try Main roads or Public streets.")

    graph_projected = ox.project_graph(graph)
    graph_undirected = _to_undirected_graph(graph_projected)
    if prune_dead_ends:
        graph_undirected = _prune_dead_ends(graph_undirected)

    nodes_projected, edges_projected = ox.graph_to_gdfs(graph_undirected, nodes=True, edges=True)
    polygon_projected = _polygon_gdf(polygon).to_crs(nodes_projected.crs).geometry.iloc[0]

    snapped_projected, meta = _build_fitted_cell_polygon(
        edges_projected=edges_projected,
        drawn_polygon_projected=polygon_projected,
        min_cell_area_m2=min_cell_area_m2,
        max_cell_area_multiple=max_cell_area_multiple,
        min_cell_inside_ratio=min_cell_inside_ratio,
        max_cell_outside_ratio=max_cell_outside_ratio,
        simplify_tolerance_m=simplify_tolerance_m,
        fit_mode=fit_mode,
        max_refinement_iterations=max_refinement_iterations,
    )

    boundary_projected = snapped_projected.boundary

    snapped_wgs84 = gpd.GeoDataFrame(geometry=[snapped_projected], crs=nodes_projected.crs).to_crs("EPSG:4326").geometry.iloc[0]
    boundary_wgs84 = gpd.GeoDataFrame(geometry=[boundary_projected], crs=nodes_projected.crs).to_crs("EPSG:4326").geometry.iloc[0]

    warning_parts: list[str] = []
    if meta.get("reduced_to_best"):
        warning_parts.append("Multiple closed components were possible, so the best-fitting component was kept instead of the largest one.")
    if target != "roads":
        warning_parts.append("Road+rail or rail-only snapping can create unusual cells where rail lines split road cells. Roads only is usually cleaner.")
    if meta["coverage_ratio"] < 0.40:
        warning_parts.append("The fitted polygon covers less than 40% of the drawn polygon. Try Cover mode, Public streets, or a larger search buffer.")
    if meta["outside_ratio"] > 0.50:
        warning_parts.append("More than half of the fitted polygon lies outside the drawn polygon. Try Tight mode or reduce max cell outside ratio.")

    return SnapResult(
        snapped_geojson=mapping(snapped_wgs84),
        snapped_boundary_geojson=mapping(boundary_wgs84),
        original_polygon_geojson=mapping(polygon),
        query_area_geojson=mapping(query_polygon_wgs84),
        algorithm="best_fit_cell_polygonizer_v5",
        network_nodes_count=int(graph_undirected.number_of_nodes()),
        network_edges_count=int(graph_undirected.number_of_edges()),
        candidate_cells_count=int(meta["candidate_cells_count"]),
        initially_selected_cells_count=int(meta["initially_selected_cells_count"]),
        final_selected_cells_count=int(meta["final_selected_cells_count"]),
        output_area_m2=float(snapped_projected.area),
        output_perimeter_m=float(snapped_projected.length),
        coordinate_count=int(_count_coordinates(snapped_projected)),
        closed_loop=snapped_projected.geom_type in {"Polygon", "MultiPolygon"},
        fit_score=float(meta["fit_score"]),
        coverage_ratio=float(meta["coverage_ratio"]),
        outside_ratio=float(meta["outside_ratio"]),
        missing_ratio=float(meta["missing_ratio"]),
        warning=" ".join(warning_parts) if warning_parts else None,
    )
