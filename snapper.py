from __future__ import annotations

from dataclasses import dataclass
from math import log, sqrt
from typing import Any, Literal

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import polygonize, unary_union

# Faster repeated runs in Streamlit/OSMnx. The first query still has to hit
# Overpass, but repeated polygons/settings can reuse OSMnx's local HTTP cache.
ox.settings.use_cache = True
ox.settings.log_console = False
ox.settings.requests_timeout = 180

SnapTarget = Literal["roads", "rails", "roads_and_rails"]
RoadTier = Literal["arterial", "main", "public", "all_drivable"]
FitMode = Literal["balanced", "tight", "cover"]

# Deliberately exclude footway/path/pedestrian/crossing/cycleway/track/steps.
# This app is drawing a road/rail polygon boundary, not a human route.
ROAD_TIERS: dict[RoadTier, set[str]] = {
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

ROAD_TIER_FALLBACKS: dict[RoadTier, list[RoadTier]] = {
    "arterial": ["arterial", "main", "public"],
    "main": ["main", "public"],
    "public": ["public"],
    "all_drivable": ["all_drivable"],
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
    holes_removed_count: int = 0
    holes_removed_area_m2: float = 0.0
    holes_filled_count: int = 0
    coverage_target: float = 0.0
    requested_road_tier: str | None = None
    road_tier_used: str | None = None
    auto_fallback_used: bool = False
    selected_road_tier: str | None = None
    selected_fit_mode: str | None = None
    selected_seed_name: str | None = None
    auto_retry_used: bool = False
    retry_attempts_count: int = 1
    coverage_rescue_added_cells: int = 0
    boundary_inside_ratio: float = 0.0
    vertices_inside_ratio: float = 0.0
    boundary_capture_distance_m: float = 0.0
    outline_cleanup_m: float = 0.0
    pre_cleanup_coordinate_count: int = 0
    post_cleanup_coordinate_count: int = 0
    outline_cleanup_applied: bool = False
    outline_cleanup_method: str | None = None
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
    to_remove = [
        (u, v, k)
        for u, v, k, attrs in filtered.edges(keys=True, data=True)
        if not _edge_is_allowed(attrs, target=target, road_tier=road_tier)
    ]
    filtered.remove_edges_from(to_remove)
    filtered.remove_nodes_from(list(nx.isolates(filtered)))
    return filtered


def _prune_dead_ends(graph: nx.MultiGraph) -> nx.MultiGraph:
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


def _polygon_parts(geom: Any) -> list[Polygon]:
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [part for part in geom.geoms if not part.is_empty and part.area > 0]
    return []


def _safe_union(polygons: list[Polygon]) -> Any:
    if not polygons:
        return Polygon()
    geom = unary_union(polygons)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def _count_interior_rings(geom: Any) -> int:
    if geom.is_empty:
        return 0
    if geom.geom_type == "Polygon":
        return len(geom.interiors)
    if geom.geom_type == "MultiPolygon":
        return sum(len(part.interiors) for part in geom.geoms)
    return 0


def _interior_ring_area_m2(geom: Any) -> float:
    if geom.is_empty:
        return 0.0
    if geom.geom_type == "Polygon":
        return float(sum(Polygon(ring).area for ring in geom.interiors))
    if geom.geom_type == "MultiPolygon":
        return float(sum(_interior_ring_area_m2(part) for part in geom.geoms))
    return 0.0


def _remove_polygon_holes(geom: Any) -> Any:
    if geom.is_empty:
        return geom
    if geom.geom_type == "Polygon":
        cleaned = Polygon(geom.exterior)
        if not cleaned.is_valid:
            cleaned = cleaned.buffer(0)
        return cleaned
    if geom.geom_type == "MultiPolygon":
        parts: list[Polygon] = []
        for part in geom.geoms:
            if part.is_empty or part.area <= 0:
                continue
            cleaned = Polygon(part.exterior)
            if not cleaned.is_valid:
                cleaned = cleaned.buffer(0)
            parts.extend(_polygon_parts(cleaned))
        cleaned_union = _safe_union(parts)
        if not cleaned_union.is_valid:
            cleaned_union = cleaned_union.buffer(0)
        return cleaned_union
    return geom


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


def _drawn_boundary_containment_ratio(candidate: Any, drawn_polygon: Polygon, sample_count: int = 64) -> float:
    """Return the share of drawn-boundary samples covered by the candidate.

    This guards against the old failure mode where a neat little internal road
    cell won the score even though it ignored a large side of the user drawing.
    A small tolerance is used because snapped roads can sit just outside the
    rough hand-drawn blue line.
    """
    if candidate.is_empty or drawn_polygon.is_empty:
        return 0.0
    drawn_area = max(float(drawn_polygon.area), 1.0)
    tolerance_m = max(8.0, min(45.0, sqrt(drawn_area) * 0.025))
    candidate_with_tolerance = candidate.buffer(tolerance_m)
    points = _sample_line(drawn_polygon.boundary, sample_count)
    if not points:
        return 0.0
    covered = sum(1 for point in points if candidate_with_tolerance.covers(point))
    return float(covered / len(points))


def _drawn_vertex_containment_ratio(candidate: Any, drawn_polygon: Polygon) -> float:
    if candidate.is_empty or drawn_polygon.is_empty:
        return 0.0
    drawn_area = max(float(drawn_polygon.area), 1.0)
    tolerance_m = max(10.0, min(60.0, sqrt(drawn_area) * 0.03))
    candidate_with_tolerance = candidate.buffer(tolerance_m)
    vertices = list(drawn_polygon.exterior.coords)[:-1]
    if not vertices:
        return 0.0
    covered = sum(1 for x, y in vertices if candidate_with_tolerance.covers(Point(x, y)))
    return float(covered / len(vertices))


def _coverage_target(mode: FitMode) -> float:
    # A snapped boundary should normally enclose most of the user drawing.
    # V11 raises these targets so a small internal road cell cannot win just
    # because it has low outside bulge.
    if mode == "tight":
        return 0.70
    if mode == "cover":
        return 0.90
    return 0.82


def _fit_weights(mode: FitMode) -> dict[str, float]:
    if mode == "tight":
        return {"coverage": 5.8, "outside": 2.8, "missing": 3.5, "area": 0.26, "boundary": 0.70, "simplicity": 0.018, "boundary_inside": 2.2, "vertices_inside": 1.2}
    if mode == "cover":
        return {"coverage": 7.0, "outside": 1.2, "missing": 4.7, "area": 0.20, "boundary": 0.58, "simplicity": 0.012, "boundary_inside": 3.2, "vertices_inside": 1.6}
    return {"coverage": 6.4, "outside": 1.7, "missing": 4.2, "area": 0.23, "boundary": 0.62, "simplicity": 0.014, "boundary_inside": 2.8, "vertices_inside": 1.4}


def _score_polygon(candidate: Any, drawn_polygon: Polygon, mode: FitMode) -> dict[str, float]:
    candidate = _remove_polygon_holes(candidate)
    if candidate.is_empty:
        return {
            "score": -1e9,
            "coverage_ratio": 0.0,
            "outside_ratio": 1.0,
            "missing_ratio": 1.0,
            "boundary_distance_m": 1e9,
            "coverage_target": float(_coverage_target(mode)),
            "boundary_inside_ratio": 0.0,
            "vertices_inside_ratio": 0.0,
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

    boundary_inside_ratio = _drawn_boundary_containment_ratio(candidate, drawn_polygon, sample_count=64)
    vertices_inside_ratio = _drawn_vertex_containment_ratio(candidate, drawn_polygon)

    coord_count = _count_coordinates(candidate)
    simplicity_penalty = coord_count / 1000.0

    weights = _fit_weights(mode)
    coverage_target = _coverage_target(mode)
    coverage_deficit = max(coverage_target - coverage_ratio, 0.0)
    coverage_floor_penalty = 12.0 * coverage_deficit + 28.0 * (coverage_deficit**2)
    if coverage_ratio < 0.55:
        coverage_floor_penalty += 7.0 * (0.55 - coverage_ratio)

    boundary_deficit = max(0.72 - boundary_inside_ratio, 0.0)
    vertex_deficit = max(0.75 - vertices_inside_ratio, 0.0)
    containment_penalty = 6.0 * boundary_deficit + 7.0 * (boundary_deficit**2) + 3.5 * vertex_deficit

    score = (
        weights["coverage"] * coverage_ratio
        + weights["boundary_inside"] * boundary_inside_ratio
        + weights["vertices_inside"] * vertices_inside_ratio
        - weights["outside"] * outside_ratio
        - weights["missing"] * missing_ratio
        - weights["area"] * area_penalty
        - weights["boundary"] * boundary_distance_norm
        - weights["simplicity"] * simplicity_penalty
        - coverage_floor_penalty
        - containment_penalty
    )

    return {
        "score": float(score),
        "coverage_ratio": float(coverage_ratio),
        "outside_ratio": float(outside_ratio),
        "missing_ratio": float(missing_ratio),
        "boundary_distance_m": float(boundary_distance_m),
        "coverage_target": float(coverage_target),
        "boundary_inside_ratio": float(boundary_inside_ratio),
        "vertices_inside_ratio": float(vertices_inside_ratio),
    }


def _best_single_component(geom: Any, drawn_polygon: Polygon, mode: FitMode) -> tuple[Any, bool, dict[str, float]]:
    geom = _remove_polygon_holes(geom)
    parts = _polygon_parts(geom)
    if not parts:
        return geom, False, _score_polygon(geom, drawn_polygon, mode)
    if len(parts) == 1:
        return parts[0], False, _score_polygon(parts[0], drawn_polygon, mode)
    scored = [(part, _score_polygon(part, drawn_polygon, mode)) for part in parts]
    best_part, best_score = max(scored, key=lambda item: item[1]["score"])
    return best_part, True, best_score



def _normalize_polygon_candidate(geom: Any, drawn_polygon: Polygon, mode: FitMode) -> tuple[Any, dict[str, float]] | None:
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    geom = _remove_polygon_holes(geom)
    geom, _, _ = _best_single_component(geom, drawn_polygon, mode)
    if geom.is_empty or geom.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    return geom, _score_polygon(geom, drawn_polygon, mode)


def _clean_outline_value(candidate: Any, score: dict[str, float], base_score: dict[str, float], base_coords: int) -> float:
    coords = max(_count_coordinates(candidate), 1)
    simplicity_gain = max(base_coords - coords, 0) / max(float(base_coords), 1.0)
    return float(
        score["score"]
        + 2.10 * simplicity_gain
        - 0.008 * coords
        - 6.0 * max(0.0, base_score["coverage_ratio"] - score["coverage_ratio"] - 0.035)
        - 4.0 * max(0.0, score["outside_ratio"] - base_score["outside_ratio"] - 0.060)
    )


def _final_outline_cleanup(
    geom: Any,
    drawn_polygon: Polygon,
    mode: FitMode,
    cleanup_tolerance_m: float,
) -> tuple[Any, dict[str, float], dict[str, Any]]:
    """Remove small road-mesh teeth while keeping the same broad road shell."""
    normalized = _normalize_polygon_candidate(geom, drawn_polygon, mode)
    if normalized is None:
        score = _score_polygon(geom, drawn_polygon, mode)
        coords = _count_coordinates(geom)
        return geom, score, {
            "outline_cleanup_m": 0.0,
            "outline_cleanup_applied": False,
            "outline_cleanup_method": None,
            "pre_clean_coordinate_count": int(coords),
            "post_clean_coordinate_count": int(coords),
        }

    base_geom, base_score = normalized
    base_coords = max(_count_coordinates(base_geom), 1)
    base_area = max(float(base_geom.area), 1.0)
    cleanup = max(0.0, float(cleanup_tolerance_m))

    candidates: list[tuple[float, int, str, float, Any, dict[str, float]]] = []

    def add_candidate(name: str, candidate: Any, tolerance: float, bonus: float = 0.0) -> None:
        normalized_candidate = _normalize_polygon_candidate(candidate, drawn_polygon, mode)
        if normalized_candidate is None:
            return
        candidate_geom, candidate_score = normalized_candidate
        coords = _count_coordinates(candidate_geom)
        if coords < 4:
            return
        # Do not allow smoothing to become a different polygon.
        if candidate_score["coverage_ratio"] + 0.055 < base_score["coverage_ratio"]:
            return
        if candidate_score["missing_ratio"] > base_score["missing_ratio"] + 0.080:
            return
        if candidate_score["outside_ratio"] > base_score["outside_ratio"] + 0.110:
            return
        if candidate_score.get("boundary_inside_ratio", 0.0) + 0.10 < base_score.get("boundary_inside_ratio", 0.0):
            return
        area_ratio = float(candidate_geom.area) / base_area
        if area_ratio < 0.78 or area_ratio > 1.27:
            return
        value = _clean_outline_value(candidate_geom, candidate_score, base_score, base_coords) + float(bonus)
        candidates.append((value, int(coords), name, float(tolerance), candidate_geom, candidate_score))

    add_candidate("base", base_geom, 0.0)

    if cleanup > 0:
        for factor in (0.80, 1.00, 1.30, 1.70, 2.20):
            tol = cleanup * factor
            try:
                add_candidate("simplify", base_geom.simplify(tol, preserve_topology=True), tol, bonus=0.04)
            except Exception:
                pass
            try:
                add_candidate("simplify_non_topology", base_geom.simplify(tol, preserve_topology=False), tol, bonus=0.06)
            except Exception:
                pass

        # Tiny morphological closing/opening removes needle-like protrusions and
        # inward notches that are caused by internal side streets.
        morph = min(max(cleanup * 0.70, 0.0), 45.0)
        if morph > 2.0:
            try:
                add_candidate("close_notches", base_geom.buffer(morph, join_style=2).buffer(-morph, join_style=2), morph, bonus=0.08)
            except Exception:
                pass
            try:
                add_candidate("remove_spikes", base_geom.buffer(-morph, join_style=2).buffer(morph, join_style=2), morph, bonus=0.08)
            except Exception:
                pass

    if not candidates:
        return base_geom, base_score, {
            "outline_cleanup_m": 0.0,
            "outline_cleanup_applied": False,
            "outline_cleanup_method": None,
            "pre_clean_coordinate_count": int(base_coords),
            "post_clean_coordinate_count": int(base_coords),
        }

    # Strongly prefer the simplest safe geometry, but avoid a very poor score.
    candidates.sort(key=lambda item: (item[1], -item[0]))
    simplest = candidates[0]
    best_value = max(candidates, key=lambda item: item[0])
    chosen = simplest if simplest[0] >= best_value[0] - 0.45 else best_value
    value, coords, method, tolerance, chosen_geom, chosen_score = chosen
    return chosen_geom, chosen_score, {
        "outline_cleanup_m": float(tolerance),
        "outline_cleanup_applied": bool(method != "base"),
        "outline_cleanup_method": None if method == "base" else method,
        "pre_clean_coordinate_count": int(base_coords),
        "post_clean_coordinate_count": int(coords),
    }

def _initial_cell_selection(
    cells: list[Polygon],
    drawn_polygon: Polygon,
    min_cell_inside_ratio: float,
    max_cell_outside_ratio: float,
) -> list[int]:
    selected: list[int] = []
    drawn_area = max(drawn_polygon.area, 1.0)
    boundary = drawn_polygon.boundary
    distance_scale = max(sqrt(drawn_area), 1.0)

    relaxed_inside_ratio = max(0.12, float(min_cell_inside_ratio) * 0.55)
    relaxed_outside_ratio = min(0.90, max(float(max_cell_outside_ratio), 0.68))

    for i, cell in enumerate(cells):
        inter_area = cell.intersection(drawn_polygon).area
        if inter_area <= 0:
            continue
        cell_area = max(cell.area, 1.0)
        inside_ratio = inter_area / cell_area
        outside_ratio = max(cell_area - inter_area, 0.0) / cell_area
        drawn_coverage = inter_area / drawn_area
        rep_inside = cell.representative_point().within(drawn_polygon)
        centroid_inside = cell.centroid.within(drawn_polygon)
        boundary_distance_norm = cell.boundary.distance(boundary) / distance_scale

        if rep_inside or centroid_inside:
            selected.append(i)
        elif inside_ratio >= relaxed_inside_ratio and outside_ratio <= relaxed_outside_ratio:
            selected.append(i)
        elif drawn_coverage >= 0.025 and outside_ratio <= 0.82:
            selected.append(i)
        elif drawn_coverage >= 0.010 and boundary_distance_norm <= 0.08:
            selected.append(i)

    if selected:
        return selected

    ranked = []
    for i, cell in enumerate(cells):
        inter_area = cell.intersection(drawn_polygon).area
        if inter_area <= 0:
            continue
        cell_area = max(cell.area, 1.0)
        drawn_coverage = inter_area / drawn_area
        inside_ratio = inter_area / cell_area
        outside_ratio = max(cell_area - inter_area, 0.0) / cell_area
        ranked.append((drawn_coverage * 3.0 + inside_ratio - 0.25 * outside_ratio, i))

    ranked.sort(reverse=True)
    return [i for _, i in ranked[: min(12, len(ranked))]]


def _candidate_seed_selections(
    cells: list[Polygon],
    drawn_polygon: Polygon,
    min_cell_inside_ratio: float,
    max_cell_outside_ratio: float,
    boundary_capture_distance_m: float,
) -> list[tuple[str, list[int]]]:
    seeds: list[tuple[str, list[int]]] = []
    seen: set[tuple[int, ...]] = set()

    def add(name: str, indices: list[int]) -> None:
        unique = sorted(set(indices))
        if not unique:
            return
        key = tuple(unique)
        if key in seen:
            return
        seen.add(key)
        seeds.append((name, unique))

    add("mesh_overlap", _initial_cell_selection(cells, drawn_polygon, min_cell_inside_ratio, max_cell_outside_ratio))

    center_indices = [
        i
        for i, cell in enumerate(cells)
        if cell.intersects(drawn_polygon)
        and (cell.representative_point().within(drawn_polygon) or cell.centroid.within(drawn_polygon))
    ]
    add("centers_inside", center_indices)

    drawn_area = max(drawn_polygon.area, 1.0)
    drawn_boundary = drawn_polygon.boundary
    distance_scale = max(sqrt(drawn_area), 1.0)
    capture = max(0.0, float(boundary_capture_distance_m))
    buffered_polygon = drawn_polygon.buffer(capture) if capture > 0 else drawn_polygon
    buffered_boundary_band = drawn_boundary.buffer(capture) if capture > 0 else drawn_boundary.buffer(1.0)

    coverage_ranked = []
    buffered_ranked = []
    boundary_ranked = []

    for i, cell in enumerate(cells):
        inter_area = cell.intersection(drawn_polygon).area
        cell_area = max(cell.area, 1.0)
        outside_ratio = max(cell_area - inter_area, 0.0) / cell_area
        drawn_coverage = inter_area / drawn_area
        boundary_distance_norm = cell.boundary.distance(drawn_boundary) / distance_scale

        if drawn_coverage >= 0.006 and outside_ratio <= 0.88:
            coverage_ranked.append((drawn_coverage, i))

        if capture > 0 and cell.intersects(buffered_polygon):
            buffered_intersection = cell.intersection(buffered_polygon).area / cell_area
            center_ok = cell.representative_point().within(buffered_polygon) or cell.centroid.within(buffered_polygon)
            near_boundary = cell.intersects(buffered_boundary_band) or cell.boundary.distance(drawn_boundary) <= capture
            if center_ok or near_boundary or buffered_intersection >= 0.25:
                # Prefer cells that are near the user's outline and not enormous outside bulges.
                rank = (1.8 * drawn_coverage) + (0.9 * buffered_intersection) - (0.40 * outside_ratio) - (0.35 * boundary_distance_norm)
                buffered_ranked.append((rank, i))

        # A separate outline seed catches the Melbourne/Paris style failure where
        # the best visual boundary requires a few cells just outside the blue line.
        if capture > 0 and cell.intersects(buffered_boundary_band):
            boundary_intersection = cell.intersection(buffered_boundary_band).area / cell_area
            rank = (1.5 * boundary_intersection) + (1.5 * drawn_coverage) - (0.25 * outside_ratio) - (0.25 * boundary_distance_norm)
            boundary_ranked.append((rank, i))

    coverage_ranked.sort(reverse=True)
    buffered_ranked.sort(reverse=True)
    boundary_ranked.sort(reverse=True)

    add("coverage_ranked", [i for _, i in coverage_ranked[: min(60, len(coverage_ranked))]])
    add("buffered_outline", [i for _, i in buffered_ranked[: min(90, len(buffered_ranked))]])
    add("boundary_band", [i for _, i in boundary_ranked[: min(70, len(boundary_ranked))]])

    return seeds


def _local_cell_refinement(
    cells: list[Polygon],
    selected_indices: list[int],
    drawn_polygon: Polygon,
    mode: FitMode,
    max_iterations: int,
) -> tuple[list[int], Any, dict[str, float], int, int]:
    selected = set(selected_indices)
    remove_count = 0
    add_count = 0
    coverage_target = _coverage_target(mode)

    def current_geometry(indices: set[int]) -> tuple[Any, dict[str, float], bool]:
        geom = _safe_union([cells[i] for i in sorted(indices)])
        geom, reduced, score = _best_single_component(geom, drawn_polygon, mode)
        return geom, score, reduced

    current_geom, current_score, _ = current_geometry(selected)
    best_value = current_score["score"]

    for _ in range(max_iterations):
        changed = False

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
                coverage_gain = trial_score["coverage_ratio"] - current_score["coverage_ratio"]
                coverage_recovery = current_score["coverage_ratio"] < coverage_target and coverage_gain > 0.01
                if trial_value > best_add_value + 1e-6 or coverage_recovery:
                    if coverage_recovery and trial_value <= best_add_value + 1e-6:
                        trial_value = best_add_value + coverage_gain
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
    outline_cleanup_m: float,
    fit_mode: FitMode,
    max_refinement_iterations: int,
    boundary_capture_distance_m: float = 0.0,
) -> tuple[Any, dict[str, Any]]:
    lines = _line_geometries_from_edges(edges_projected)
    if not lines:
        raise ValueError("No usable road/rail line geometry was found after filtering.")

    noded_linework = unary_union(lines)
    drawn_area = max(float(drawn_polygon_projected.area), 1.0)
    max_cell_area = drawn_area * float(max_cell_area_multiple)

    raw_cells = []
    capture = max(0.0, float(boundary_capture_distance_m))
    drawn_boundary = drawn_polygon_projected.boundary
    buffered_polygon = drawn_polygon_projected.buffer(capture) if capture > 0 else drawn_polygon_projected
    boundary_band = drawn_boundary.buffer(capture) if capture > 0 else drawn_boundary.buffer(1.0)

    for cell in polygonize(noded_linework):
        if cell.is_empty or cell.area < float(min_cell_area_m2):
            continue
        if cell.area > max_cell_area:
            continue
        intersects_input = cell.intersects(drawn_polygon_projected)
        near_input = capture > 0 and (cell.intersects(buffered_polygon) or cell.intersects(boundary_band) or cell.distance(drawn_polygon_projected) <= capture)
        if not (intersects_input or near_input):
            continue
        raw_cells.append(cell)

    if not raw_cells:
        raise ValueError("No closed road cells were found near the drawn polygon. Try moving Boundary detail right or Fit right.")

    seed_sets = _candidate_seed_selections(
        cells=raw_cells,
        drawn_polygon=drawn_polygon_projected,
        min_cell_inside_ratio=min_cell_inside_ratio,
        max_cell_outside_ratio=max_cell_outside_ratio,
        boundary_capture_distance_m=boundary_capture_distance_m,
    )
    if not seed_sets:
        raise ValueError("Road cells were found, but none matched the drawn polygon closely enough.")

    best_candidate: tuple[list[int], Any, dict[str, float], int, int, str] | None = None
    for seed_name, seed_indices in seed_sets:
        try:
            trial_indices, trial_geom, trial_score, trial_removed, trial_added = _local_cell_refinement(
                cells=raw_cells,
                selected_indices=seed_indices,
                drawn_polygon=drawn_polygon_projected,
                mode=fit_mode,
                max_iterations=max_refinement_iterations,
            )
        except Exception:
            continue
        if best_candidate is None or trial_score["score"] > best_candidate[2]["score"]:
            best_candidate = (trial_indices, trial_geom, trial_score, trial_removed, trial_added, seed_name)

    if best_candidate is None:
        raise ValueError("Road cells were found, but none could be refined into a usable polygon.")

    final_indices, fitted, score, removed, added, selected_seed_name = best_candidate

    reduced_to_best = False
    holes_removed_count = 0
    holes_removed_area_m2 = 0.0

    fitted, reduced_to_best, score = _best_single_component(fitted, drawn_polygon_projected, fit_mode)

    holes_removed_count += _count_interior_rings(fitted)
    holes_removed_area_m2 += _interior_ring_area_m2(fitted)
    fitted = _remove_polygon_holes(fitted)
    fitted, reduced_after_holes, score = _best_single_component(fitted, drawn_polygon_projected, fit_mode)
    reduced_to_best = reduced_to_best or reduced_after_holes
    score = _score_polygon(fitted, drawn_polygon_projected, fit_mode)

    if simplify_tolerance_m > 0:
        fitted = fitted.simplify(float(simplify_tolerance_m), preserve_topology=True)
        if not fitted.is_valid:
            fitted = fitted.buffer(0)
        holes_removed_count += _count_interior_rings(fitted)
        holes_removed_area_m2 += _interior_ring_area_m2(fitted)
        fitted = _remove_polygon_holes(fitted)
        fitted, reduced_again, score = _best_single_component(fitted, drawn_polygon_projected, fit_mode)
        reduced_to_best = reduced_to_best or reduced_again
        score = _score_polygon(fitted, drawn_polygon_projected, fit_mode)

    cleanup_meta = {
        "outline_cleanup_m": 0.0,
        "outline_cleanup_applied": False,
        "outline_cleanup_method": None,
        "pre_clean_coordinate_count": int(_count_coordinates(fitted)),
        "post_clean_coordinate_count": int(_count_coordinates(fitted)),
    }
    if outline_cleanup_m and outline_cleanup_m > 0:
        fitted, score, cleanup_meta = _final_outline_cleanup(
            fitted,
            drawn_polygon_projected,
            fit_mode,
            cleanup_tolerance_m=float(outline_cleanup_m),
        )

    if fitted.is_empty:
        raise ValueError("The snapped polygon became empty after fitting. Try a lower Boundary detail value.")

    meta = {
        "candidate_cells_count": len(raw_cells),
        "initially_selected_cells_count": len(seed_sets),
        "final_selected_cells_count": len(final_indices),
        "selected_seed_name": selected_seed_name,
        "fit_score": score["score"],
        "coverage_ratio": score["coverage_ratio"],
        "outside_ratio": score["outside_ratio"],
        "missing_ratio": score["missing_ratio"],
        "boundary_distance_m": score["boundary_distance_m"],
        "coverage_target": score.get("coverage_target", _coverage_target(fit_mode)),
        "boundary_inside_ratio": score.get("boundary_inside_ratio", 0.0),
        "vertices_inside_ratio": score.get("vertices_inside_ratio", 0.0),
        "boundary_capture_distance_m": float(boundary_capture_distance_m),
        "holes_removed_count": holes_removed_count,
        "holes_removed_area_m2": holes_removed_area_m2,
        "removed_cells_count": removed,
        "added_cells_count": added,
        "reduced_to_best": reduced_to_best,
        "outline_cleanup_m": float(cleanup_meta.get("outline_cleanup_m", 0.0)),
        "outline_cleanup_applied": bool(cleanup_meta.get("outline_cleanup_applied", False)),
        "outline_cleanup_method": cleanup_meta.get("outline_cleanup_method"),
        "pre_clean_coordinate_count": int(cleanup_meta.get("pre_clean_coordinate_count", _count_coordinates(fitted))),
        "post_clean_coordinate_count": int(cleanup_meta.get("post_clean_coordinate_count", _count_coordinates(fitted))),
    }
    return fitted, meta




def _coverage_target_for_mode(mode: FitMode) -> float:
    return _coverage_target(mode)


def _outside_ceiling_for_mode(mode: FitMode) -> float:
    if mode == "tight":
        return 0.50
    if mode == "cover":
        return 0.76
    return 0.62


def _tier_fallbacks(road_tier: RoadTier, target: SnapTarget) -> list[RoadTier]:
    if target == "rails":
        return [road_tier]
    return ROAD_TIER_FALLBACKS.get(road_tier, [road_tier])


def _broadest_query_tier(road_tier: RoadTier, target: SnapTarget) -> RoadTier:
    # V11 speed/default-cleanliness choice: query the road tier selected by the
    # Boundary detail slider. Earlier versions queried the broadest fallback
    # tier up front, which was slower and introduced side-street jaggedness.
    # Move Boundary detail right to query normal public streets when needed.
    return road_tier


def _choice_score_for_output(geom: Any, drawn_polygon: Polygon, mode: FitMode, tier_penalty: float = 0.0) -> float:
    scored = _score_polygon(geom, drawn_polygon, mode)
    coverage = scored["coverage_ratio"]
    outside = scored["outside_ratio"]
    missing = scored["missing_ratio"]
    target = _coverage_target(mode)
    outside_ceiling = _outside_ceiling_for_mode(mode)

    boundary_inside = scored.get("boundary_inside_ratio", 0.0)
    vertices_inside = scored.get("vertices_inside_ratio", 0.0)

    value = (
        10.0 * coverage
        + 2.6 * boundary_inside
        + 1.4 * vertices_inside
        - 2.2 * outside
        - 4.4 * missing
        + 0.35 * scored["score"]
        - tier_penalty
    )

    if coverage < target:
        gap = target - coverage
        value -= 7.0 * gap + 14.0 * (gap**2)
    if coverage < 0.60 and outside < 0.25:
        # Small internal cells often have attractive outside-bulge scores.
        # Make that failure mode very expensive.
        value -= 4.0 + 8.0 * (0.60 - coverage)
    if boundary_inside < 0.65:
        value -= 4.5 * (0.65 - boundary_inside)
    if vertices_inside < 0.75:
        value -= 2.5 * (0.75 - vertices_inside)
    if outside > outside_ceiling:
        bulge = outside - outside_ceiling
        value -= 3.0 * bulge + 5.0 * (bulge**2)

    return float(value)

def _attempt_specs_for_simple_ui(
    *,
    target: SnapTarget,
    road_tier: RoadTier,
    fit_mode: FitMode,
    min_cell_area_m2: float,
    max_cell_area_multiple: float,
    min_cell_inside_ratio: float,
    max_cell_outside_ratio: float,
    simplify_tolerance_m: float,
    outline_cleanup_m: float,
    max_refinement_iterations: int,
) -> list[dict[str, Any]]:
    """Hidden attempts behind the simple two-slider UI."""
    specs: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add(
        *,
        tier: RoadTier,
        mode: FitMode,
        label: str,
        min_area_factor: float = 1.0,
        max_area_factor: float = 1.0,
        inside_factor: float = 1.0,
        outside_bonus: float = 0.0,
        simplify_factor: float = 1.0,
        cleanup_factor: float = 1.0,
        iteration_bonus: int = 0,
        tier_penalty: float = 0.0,
    ) -> None:
        spec = {
            "tier": tier,
            "mode": mode,
            "label": label,
            "min_cell_area_m2": max(80.0, float(min_cell_area_m2) * min_area_factor),
            "max_cell_area_multiple": min(10.0, float(max_cell_area_multiple) * max_area_factor),
            "min_cell_inside_ratio": max(0.05, min(0.95, float(min_cell_inside_ratio) * inside_factor)),
            "max_cell_outside_ratio": max(0.05, min(0.95, float(max_cell_outside_ratio) + outside_bonus)),
            "simplify_tolerance_m": max(1.0, float(simplify_tolerance_m) * simplify_factor),
            "outline_cleanup_m": max(0.0, float(outline_cleanup_m) * cleanup_factor),
            "max_refinement_iterations": int(max_refinement_iterations) + int(iteration_bonus),
            "tier_penalty": float(tier_penalty),
        }
        key = (
            spec["tier"], spec["mode"], round(spec["min_cell_area_m2"], 1),
            round(spec["max_cell_area_multiple"], 2), round(spec["min_cell_inside_ratio"], 2),
            round(spec["max_cell_outside_ratio"], 2), round(spec["simplify_tolerance_m"], 1), round(spec["outline_cleanup_m"], 1),
        )
        if key not in seen:
            seen.add(key)
            specs.append(spec)

    add(tier=road_tier, mode=fit_mode, label="requested")

    if fit_mode == "tight":
        add(
            tier=road_tier, mode="balanced", label="coverage_guard_same_tier",
            min_area_factor=0.70, max_area_factor=1.45, inside_factor=0.75,
            outside_bonus=0.16, simplify_factor=0.95, cleanup_factor=1.05, iteration_bonus=6, tier_penalty=0.04,
        )
    elif fit_mode == "balanced":
        add(
            tier=road_tier, mode="cover", label="coverage_guard_same_tier",
            min_area_factor=0.70, max_area_factor=1.35, inside_factor=0.82,
            outside_bonus=0.12, simplify_factor=0.95, cleanup_factor=1.05, iteration_bonus=6, tier_penalty=0.04,
        )

    for idx, tier in enumerate(_tier_fallbacks(road_tier, target)):
        if tier == road_tier:
            continue
        penalty = 0.12 + idx * 0.05
        add(
            tier=tier, mode="balanced" if fit_mode != "cover" else "cover", label=f"broader_{tier}",
            min_area_factor=0.55, max_area_factor=1.70, inside_factor=0.65,
            outside_bonus=0.22, simplify_factor=0.90, cleanup_factor=1.15, iteration_bonus=10, tier_penalty=penalty,
        )
        add(
            tier=tier, mode="cover", label=f"coverage_guard_{tier}",
            min_area_factor=0.42, max_area_factor=2.20, inside_factor=0.50,
            outside_bonus=0.32, simplify_factor=0.82, cleanup_factor=1.20, iteration_bonus=14, tier_penalty=penalty + 0.06,
        )

    return specs[:4]


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
    outline_cleanup_m: float | None = None,
    prune_dead_ends: bool = True,
    fit_mode: FitMode = "balanced",
    max_refinement_iterations: int = 30,
    boundary_capture_distance_m: float | None = None,
) -> SnapResult:
    """Snap a drawn polygon to a coverage-guarded road/rail polygon."""
    polygon = _ensure_polygon_from_geojson(drawn_geojson)
    query_polygon_wgs84 = _buffer_polygon_meters(polygon, buffer_m=search_buffer_m)
    target_coverage = _coverage_target_for_mode(fit_mode)
    if boundary_capture_distance_m is None:
        # Hidden outline allowance. This lets the snapped boundary move to nearby
        # enclosing roads rather than only using cells that intersect the blue polygon.
        fit_capture_factor = {"tight": 0.22, "balanced": 0.32, "cover": 0.42}.get(fit_mode, 0.32)
        boundary_capture_distance_m = max(60.0, min(240.0, float(search_buffer_m) * fit_capture_factor))
    if outline_cleanup_m is None:
        outline_cleanup_m = max(8.0, float(simplify_tolerance_m) * 1.35)

    specs = _attempt_specs_for_simple_ui(
        target=target,
        road_tier=road_tier,
        fit_mode=fit_mode,
        min_cell_area_m2=min_cell_area_m2,
        max_cell_area_multiple=max_cell_area_multiple,
        min_cell_inside_ratio=min_cell_inside_ratio,
        max_cell_outside_ratio=max_cell_outside_ratio,
        simplify_tolerance_m=simplify_tolerance_m,
        outline_cleanup_m=float(outline_cleanup_m),
        max_refinement_iterations=max_refinement_iterations,
    )

    # V11 speed-up: query the clean requested road tier first. Only if a later
    # fallback is actually attempted do we query the broader tier. This avoids
    # loading all residential streets for easy main-road cases.
    projected_graph_cache: dict[RoadTier, nx.MultiDiGraph | nx.MultiGraph] = {}

    def projected_graph_for_tier(query_tier: RoadTier) -> nx.MultiDiGraph | nx.MultiGraph:
        if query_tier not in projected_graph_cache:
            custom_filter = _custom_filter_for_target(target=target, road_tier=query_tier)
            downloaded = ox.graph_from_polygon(
                query_polygon_wgs84,
                network_type="all",
                custom_filter=custom_filter,
                simplify=True,
                retain_all=True,
                truncate_by_edge=True,
            )
            if downloaded.number_of_nodes() == 0 or downloaded.number_of_edges() == 0:
                raise ValueError("no matching road/rail edges found")
            projected_graph_cache[query_tier] = ox.project_graph(downloaded)
        return projected_graph_cache[query_tier]

    candidates: list[dict[str, Any]] = []
    attempt_errors: list[str] = []

    for attempt_number, spec in enumerate(specs, start=1):
        try:
            graph_projected_downloaded = projected_graph_for_tier(spec["tier"])
            graph_attempt = _filter_graph_edges(graph_projected_downloaded, target=target, road_tier=spec["tier"])
            if graph_attempt.number_of_nodes() == 0 or graph_attempt.number_of_edges() == 0:
                raise ValueError("no edges after filtering")

            graph_undirected = _to_undirected_graph(graph_attempt)
            if prune_dead_ends:
                graph_undirected = _prune_dead_ends(graph_undirected)

            if graph_undirected.number_of_nodes() == 0 or graph_undirected.number_of_edges() == 0:
                raise ValueError("no cyclic edges after pruning")

            nodes_projected, edges_projected = ox.graph_to_gdfs(graph_undirected, nodes=True, edges=True)
            polygon_projected = _polygon_gdf(polygon).to_crs(nodes_projected.crs).geometry.iloc[0]

            snapped_projected, meta = _build_fitted_cell_polygon(
                edges_projected=edges_projected,
                drawn_polygon_projected=polygon_projected,
                min_cell_area_m2=float(spec["min_cell_area_m2"]),
                max_cell_area_multiple=float(spec["max_cell_area_multiple"]),
                min_cell_inside_ratio=float(spec["min_cell_inside_ratio"]),
                max_cell_outside_ratio=float(spec["max_cell_outside_ratio"]),
                simplify_tolerance_m=float(spec["simplify_tolerance_m"]),
                outline_cleanup_m=float(spec.get("outline_cleanup_m", outline_cleanup_m)),
                fit_mode=spec["mode"],
                max_refinement_iterations=int(spec["max_refinement_iterations"]),
                boundary_capture_distance_m=float(boundary_capture_distance_m),
            )

            choice_score = _choice_score_for_output(
                snapped_projected, polygon_projected, spec["mode"], tier_penalty=float(spec["tier_penalty"])
            )

            if attempt_number == 1 and meta["coverage_ratio"] >= target_coverage and meta["outside_ratio"] <= _outside_ceiling_for_mode(fit_mode):
                choice_score += 0.25

            candidates.append({
                "choice_score": choice_score,
                "attempt_number": attempt_number,
                "spec": spec,
                "graph": graph_undirected,
                "nodes": nodes_projected,
                "snapped_projected": snapped_projected,
                "meta": meta,
            })

            if (
                attempt_number <= 2
                and spec["tier"] == road_tier
                and meta["coverage_ratio"] >= target_coverage
                and meta["outside_ratio"] <= _outside_ceiling_for_mode(fit_mode)
                and meta.get("boundary_inside_ratio", 0.0) >= 0.72
                and int(_count_coordinates(snapped_projected)) <= 90
            ):
                break
        except Exception as exc:  # noqa: BLE001
            attempt_errors.append(f"{spec['label']} / {spec['tier']}: {exc}")
            continue

    if not candidates:
        details = "; ".join(attempt_errors[:5])
        raise ValueError(
            "No clean closed road polygon could be formed. Move Boundary detail right, move Fit right, or draw a slightly larger polygon. " + details
        )

    best = max(candidates, key=lambda item: item["choice_score"])
    spec = best["spec"]
    meta = best["meta"]
    snapped_projected = best["snapped_projected"]
    graph_undirected = best["graph"]
    nodes_projected = best["nodes"]

    boundary_projected = snapped_projected.boundary
    snapped_wgs84 = gpd.GeoDataFrame(geometry=[snapped_projected], crs=nodes_projected.crs).to_crs("EPSG:4326").geometry.iloc[0]
    boundary_wgs84 = gpd.GeoDataFrame(geometry=[boundary_projected], crs=nodes_projected.crs).to_crs("EPSG:4326").geometry.iloc[0]

    auto_retry_used = bool(best["attempt_number"] != 1 or spec["tier"] != road_tier or spec["mode"] != fit_mode)

    warning_parts: list[str] = []
    if auto_retry_used:
        warning_parts.append("The first pass looked too small, so V11 automatically tried a coverage-guarded road set.")
    if meta.get("outline_cleanup_applied"):
        warning_parts.append("V11 cleaned small spikes/dents from the outer outline to reduce jaggy edges.")
    if spec["tier"] != road_tier:
        warning_parts.append(f"Used {spec['tier']} roads internally because {road_tier} roads alone did not form a good enclosing loop.")
    if meta.get("reduced_to_best"):
        warning_parts.append("Multiple closed components were possible, so the best-fitting component was kept.")
    if target != "roads":
        warning_parts.append("Road+rail or rail-only snapping can create unusual cells. Roads only is usually cleaner.")
    if meta["coverage_ratio"] < target_coverage:
        warning_parts.append(f"The fitted polygon still covers less than the internal target ({target_coverage:.0%}). Move Fit right or Boundary detail right if it looks too small.")
    if meta.get("boundary_inside_ratio", 0.0) < 0.60:
        warning_parts.append("The fitted polygon does not contain enough of the blue outline. Move Fit right if it looks too small.")
    if meta["outside_ratio"] > max(0.60, _outside_ceiling_for_mode(fit_mode) + 0.15):
        warning_parts.append("The fitted polygon has a large outside bulge. Move Fit left if this looks wrong.")

    return SnapResult(
        snapped_geojson=mapping(snapped_wgs84),
        snapped_boundary_geojson=mapping(boundary_wgs84),
        original_polygon_geojson=mapping(polygon),
        query_area_geojson=mapping(query_polygon_wgs84),
        algorithm="smoother_fast_outline_polygonizer_v11",
        network_nodes_count=int(graph_undirected.number_of_nodes()),
        network_edges_count=int(graph_undirected.number_of_edges()),
        candidate_cells_count=int(meta["candidate_cells_count"]),
        initially_selected_cells_count=int(meta["initially_selected_cells_count"]),
        final_selected_cells_count=int(meta["final_selected_cells_count"]),
        output_area_m2=float(snapped_projected.area),
        output_perimeter_m=float(snapped_projected.length),
        coordinate_count=int(_count_coordinates(snapped_projected)),
        closed_loop=snapped_projected.geom_type in {"Polygon", "MultiPolygon"},
        fit_score=float(best["choice_score"]),
        coverage_ratio=float(meta["coverage_ratio"]),
        outside_ratio=float(meta["outside_ratio"]),
        missing_ratio=float(meta["missing_ratio"]),
        holes_removed_count=int(meta.get("holes_removed_count", meta.get("holes_filled_count", 0))),
        holes_removed_area_m2=float(meta.get("holes_removed_area_m2", 0.0)),
        holes_filled_count=int(meta.get("holes_filled_count", meta.get("holes_removed_count", 0))),
        requested_road_tier=str(road_tier),
        road_tier_used=str(spec["tier"]),
        auto_fallback_used=auto_retry_used,
        warning=" ".join(warning_parts) if warning_parts else None,
        coverage_target=float(target_coverage),
        selected_road_tier=str(spec["tier"]),
        selected_fit_mode=str(spec["mode"]),
        auto_retry_used=auto_retry_used,
        retry_attempts_count=int(len(candidates)),
        coverage_rescue_added_cells=int(meta.get("added_cells_count", 0)),
        boundary_inside_ratio=float(meta.get("boundary_inside_ratio", 0.0)),
        vertices_inside_ratio=float(meta.get("vertices_inside_ratio", 0.0)),
        boundary_capture_distance_m=float(boundary_capture_distance_m),
        outline_cleanup_m=float(meta.get("outline_cleanup_m", outline_cleanup_m or 0.0)),
        pre_cleanup_coordinate_count=int(meta.get("pre_clean_coordinate_count", _count_coordinates(snapped_projected))),
        post_cleanup_coordinate_count=int(meta.get("post_clean_coordinate_count", _count_coordinates(snapped_projected))),
        outline_cleanup_applied=bool(meta.get("outline_cleanup_applied", False)),
        outline_cleanup_method=meta.get("outline_cleanup_method"),
    )
