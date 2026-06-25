from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
from shapely.geometry import LineString, MultiLineString, Point, Polygon, mapping, shape
from shapely.ops import linemerge, unary_union

SnapTarget = Literal["roads", "rails", "roads_and_rails"]


@dataclass
class SnapResult:
    snapped_line_geojson: dict[str, Any]
    original_polygon_geojson: dict[str, Any]
    query_area_geojson: dict[str, Any]
    network_nodes_count: int
    network_edges_count: int
    control_points_count: int
    mean_snap_distance_m: float
    max_snap_distance_m: float
    output_length_m: float
    route_piece_count: int
    closed_loop: bool
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


def _polygon_gdf(polygon: Polygon) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:4326")


def _buffer_polygon_meters(polygon: Polygon, buffer_m: float) -> Polygon:
    """Create a WGS84 query buffer using a local metric projection."""
    polygon_wgs84 = _polygon_gdf(polygon)
    polygon_projected = ox.projection.project_gdf(polygon_wgs84)
    buffered_projected = polygon_projected.geometry.iloc[0].buffer(buffer_m)
    buffered_wgs84 = gpd.GeoDataFrame(geometry=[buffered_projected], crs=polygon_projected.crs).to_crs(
        "EPSG:4326"
    )
    return buffered_wgs84.geometry.iloc[0]


def _custom_filter_for_target(target: SnapTarget) -> str | list[str]:
    road_filter = '["highway"]'
    rail_filter = '["railway"~"rail|light_rail|subway|tram|narrow_gauge"]'

    if target == "roads":
        return road_filter
    if target == "rails":
        return rail_filter
    return [road_filter, rail_filter]


def _to_undirected_graph(graph: nx.MultiDiGraph) -> nx.MultiGraph:
    """
    Convert to undirected for snapping.

    For this use case, the red outline is visual linework. It should not obey one-way streets.
    """
    try:
        return ox.convert.to_undirected(graph)
    except Exception:
        return graph.to_undirected()


def _densify_line(line: LineString, spacing_m: float) -> list[Point]:
    """Sample points along a projected polygon boundary, excluding the duplicate closing point."""
    if line.length <= 0:
        return []

    spacing_m = max(float(spacing_m), 1.0)
    distances = list(np.arange(0, line.length, spacing_m))
    if len(distances) < 4:
        distances = list(np.linspace(0, line.length, 5)[:-1])

    return [line.interpolate(distance) for distance in distances]


def _downsample_points(points: list[Point], max_points: int) -> tuple[list[Point], bool]:
    """Limit control points so cyclic routing stays fast on Streamlit Cloud."""
    if len(points) <= max_points:
        return points, False

    idx = np.linspace(0, len(points) - 1, max_points).round().astype(int)
    idx = sorted(set(int(i) for i in idx))
    return [points[i] for i in idx], True


def _candidate_nodes_for_point(
    point: Point,
    nodes: gpd.GeoDataFrame,
    candidate_count: int,
    max_snap_distance_m: float | None,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Find nearest graph nodes for a control point.

    Returns candidates and whether all candidates were beyond max_snap_distance_m.
    """
    distances = nodes.geometry.distance(point)
    nearest = distances.nsmallest(max(1, int(candidate_count)))

    candidates: list[dict[str, Any]] = []
    for node_id, distance_m in nearest.items():
        distance_value = float(distance_m)
        if max_snap_distance_m is None or distance_value <= max_snap_distance_m:
            candidates.append({"node": node_id, "distance_m": distance_value})

    if candidates:
        return candidates, False

    # Soft fallback: keep the nearest candidate, but warn the user. Without this, one bad
    # point can make the whole loop impossible.
    node_id = nearest.index[0]
    return [{"node": node_id, "distance_m": float(nearest.iloc[0])}], True


def _shortest_path_length_cached(
    graph: nx.Graph,
    source: Any,
    target: Any,
    cache: dict[tuple[Any, Any], float],
) -> float:
    if source == target:
        return 0.0

    key = (source, target) if str(source) <= str(target) else (target, source)
    if key in cache:
        return cache[key]

    try:
        length = float(nx.shortest_path_length(graph, source=source, target=target, weight="length"))
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        length = float("inf")

    cache[key] = length
    return length


def _shortest_path_nodes(graph: nx.Graph, source: Any, target: Any) -> list[Any]:
    if source == target:
        return [source]
    return nx.shortest_path(graph, source=source, target=target, weight="length")


def _select_closed_node_sequence(
    graph: nx.Graph,
    candidates_by_point: list[list[dict[str, Any]]],
    boundary_closeness_weight: float,
) -> tuple[list[Any], float, float, bool]:
    """
    Choose one candidate node per polygon control point.

    This is a cyclic dynamic program. It tries all possible first candidates, walks around
    the boundary, then adds the cost of closing the last candidate back to the first.

    Score = network route length + boundary_closeness_weight * snap distance
    """
    if len(candidates_by_point) < 3:
        raise ValueError("Not enough control points to build a loop.")

    length_cache: dict[tuple[Any, Any], float] = {}
    best_score = float("inf")
    best_nodes: list[Any] = []
    best_snap_sum = float("inf")
    best_route_length = float("inf")
    closed = False

    first_candidates = candidates_by_point[0]

    for first_candidate in first_candidates:
        first_node = first_candidate["node"]
        first_snap = float(first_candidate["distance_m"])

        # State value: node -> (score_so_far, selected_nodes, snap_sum, route_length_sum)
        states: dict[Any, tuple[float, list[Any], float, float]] = {
            first_node: (
                boundary_closeness_weight * first_snap,
                [first_node],
                first_snap,
                0.0,
            )
        }

        for point_index in range(1, len(candidates_by_point)):
            next_states: dict[Any, tuple[float, list[Any], float, float]] = {}

            for prev_node, (prev_score, prev_path, prev_snap_sum, prev_route_sum) in states.items():
                for candidate in candidates_by_point[point_index]:
                    node = candidate["node"]
                    snap_distance = float(candidate["distance_m"])
                    transition_length = _shortest_path_length_cached(
                        graph, prev_node, node, length_cache
                    )
                    if not np.isfinite(transition_length):
                        continue

                    score = prev_score + transition_length + boundary_closeness_weight * snap_distance
                    snap_sum = prev_snap_sum + snap_distance
                    route_sum = prev_route_sum + transition_length
                    path = prev_path + [node]

                    current_best = next_states.get(node)
                    if current_best is None or score < current_best[0]:
                        next_states[node] = (score, path, snap_sum, route_sum)

            if not next_states:
                break
            states = next_states

        if not states:
            continue

        for last_node, (score, path, snap_sum, route_sum) in states.items():
            close_length = _shortest_path_length_cached(graph, last_node, first_node, length_cache)
            if not np.isfinite(close_length):
                continue

            total_score = score + close_length
            total_route_length = route_sum + close_length

            if total_score < best_score:
                best_score = total_score
                best_nodes = path
                best_snap_sum = snap_sum
                best_route_length = total_route_length
                closed = True

    if not best_nodes:
        # Fallback: greedy nearest connected path. This is not ideal, but it gives a useful
        # error path if the network is fragmented.
        greedy_nodes = [candidates_by_point[0][0]["node"]]
        snap_sum = float(candidates_by_point[0][0]["distance_m"])
        route_sum = 0.0
        for candidates in candidates_by_point[1:]:
            prev = greedy_nodes[-1]
            best_candidate = None
            best_cost = float("inf")
            for candidate in candidates:
                node = candidate["node"]
                length = _shortest_path_length_cached(graph, prev, node, length_cache)
                cost = length + boundary_closeness_weight * float(candidate["distance_m"])
                if np.isfinite(cost) and cost < best_cost:
                    best_cost = cost
                    best_candidate = (node, float(candidate["distance_m"]), length)
            if best_candidate is None:
                continue
            greedy_nodes.append(best_candidate[0])
            snap_sum += best_candidate[1]
            route_sum += best_candidate[2]

        if len(greedy_nodes) < 2:
            raise ValueError(
                "Could not find a connected road/rail sequence. Try roads only, a larger search buffer, "
                "or a larger max snap distance."
            )

        return greedy_nodes, snap_sum, route_sum, False

    return best_nodes, best_snap_sum, best_route_length, closed


def _edge_geometry_between(graph: nx.Graph, nodes: gpd.GeoDataFrame, u: Any, v: Any) -> LineString | None:
    if u == v:
        return None

    edge_data = graph.get_edge_data(u, v)
    if edge_data is None:
        return None

    # MultiGraph edge data is usually {key: attrs}. Simple Graph edge data is attrs.
    if isinstance(edge_data, dict) and edge_data and all(isinstance(value, dict) for value in edge_data.values()):
        attrs = min(edge_data.values(), key=lambda item: float(item.get("length", 0.0)))
    else:
        attrs = edge_data

    geometry = attrs.get("geometry") if isinstance(attrs, dict) else None
    if geometry is not None and not geometry.is_empty:
        if geometry.geom_type == "LineString":
            return geometry
        if geometry.geom_type == "MultiLineString":
            try:
                merged = linemerge(geometry)
                if merged.geom_type == "LineString":
                    return merged
            except Exception:
                pass

    try:
        return LineString([nodes.loc[u].geometry, nodes.loc[v].geometry])
    except Exception:
        return None


def _route_node_sequence_to_linework(
    graph: nx.Graph,
    nodes: gpd.GeoDataFrame,
    selected_nodes: list[Any],
    close_loop: bool,
) -> tuple[LineString | MultiLineString, int]:
    """Convert selected control nodes into actual shortest-path road/rail linework."""
    if len(selected_nodes) < 2:
        raise ValueError("The selected route has fewer than two nodes.")

    ordered_pairs = list(zip(selected_nodes[:-1], selected_nodes[1:]))
    if close_loop:
        ordered_pairs.append((selected_nodes[-1], selected_nodes[0]))

    route_lines: list[LineString] = []

    for source, target in ordered_pairs:
        if source == target:
            continue
        try:
            route_nodes = _shortest_path_nodes(graph, source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue

        for u, v in zip(route_nodes[:-1], route_nodes[1:]):
            geom = _edge_geometry_between(graph, nodes, u, v)
            if geom is not None and not geom.is_empty and geom.length > 0:
                route_lines.append(geom)

    if not route_lines:
        raise ValueError(
            "No road/rail linework was produced. Try a larger search buffer or larger max snap distance."
        )

    # Dissolve duplicate edges and merge connected pieces. This usually becomes a closed LineString.
    dissolved = unary_union(route_lines)
    try:
        merged = linemerge(dissolved)
    except Exception:
        merged = dissolved

    if merged.geom_type == "LineString":
        return merged, len(route_lines)
    if merged.geom_type == "MultiLineString":
        return merged, len(route_lines)

    # GeometryCollection fallback.
    pieces = [geom for geom in getattr(merged, "geoms", []) if geom.geom_type == "LineString"]
    if pieces:
        return MultiLineString(pieces), len(route_lines)

    return MultiLineString(route_lines), len(route_lines)


def snap_polygon_to_closed_network_loop(
    drawn_geojson: dict[str, Any],
    target: SnapTarget = "roads",
    search_buffer_m: float = 250,
    control_spacing_m: float = 60,
    max_snap_distance_m: float | None = 150,
    candidate_count: int = 5,
    boundary_closeness_weight: float = 6,
    max_control_points: int = 70,
) -> SnapResult:
    """
    Snap a user-drawn polygon boundary to a nearby connected road/rail loop.

    V3 behavior:
    - Samples the polygon boundary into control points.
    - Finds several nearby road/rail graph nodes for each control point.
    - Chooses a sequence that is both close to the boundary and can close back to the start.
    - Routes between chosen nodes along the OSM network, so the output uses real linework.

    This is closer to lightweight map matching than simple nearest-line snapping.
    """
    polygon = _ensure_polygon_from_geojson(drawn_geojson)
    query_polygon_wgs84 = _buffer_polygon_meters(polygon, buffer_m=search_buffer_m)

    custom_filter = _custom_filter_for_target(target)
    graph = ox.graph_from_polygon(
        query_polygon_wgs84,
        network_type="all",
        custom_filter=custom_filter,
        simplify=True,
        retain_all=True,
        truncate_by_edge=True,
    )

    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        raise ValueError(
            "No road/rail network was found near this polygon. Try increasing the search buffer."
        )

    graph_projected = ox.project_graph(graph)
    graph_route = _to_undirected_graph(graph_projected)
    nodes_projected, edges_projected = ox.graph_to_gdfs(graph_route, nodes=True, edges=True)

    polygon_projected = _polygon_gdf(polygon).to_crs(nodes_projected.crs)
    boundary_projected = polygon_projected.geometry.iloc[0].boundary

    control_points = _densify_line(boundary_projected, spacing_m=control_spacing_m)
    control_points, was_downsampled = _downsample_points(control_points, max_control_points)

    if len(control_points) < 4:
        raise ValueError(
            "The polygon is too small to form a useful loop. Try drawing a larger polygon or lowering control spacing."
        )

    candidates_by_point: list[list[dict[str, Any]]] = []
    soft_fallback_count = 0
    all_candidate_distances: list[float] = []

    for point in control_points:
        candidates, used_soft_fallback = _candidate_nodes_for_point(
            point=point,
            nodes=nodes_projected,
            candidate_count=candidate_count,
            max_snap_distance_m=max_snap_distance_m,
        )
        candidates_by_point.append(candidates)
        soft_fallback_count += int(used_soft_fallback)
        all_candidate_distances.append(float(candidates[0]["distance_m"]))

    selected_nodes, snap_sum, route_length, closed = _select_closed_node_sequence(
        graph=graph_route,
        candidates_by_point=candidates_by_point,
        boundary_closeness_weight=boundary_closeness_weight,
    )

    snapped_projected, route_piece_count = _route_node_sequence_to_linework(
        graph=graph_route,
        nodes=nodes_projected,
        selected_nodes=selected_nodes,
        close_loop=closed,
    )

    snapped_wgs84 = gpd.GeoDataFrame(geometry=[snapped_projected], crs=nodes_projected.crs).to_crs(
        "EPSG:4326"
    ).geometry.iloc[0]

    warning_parts: list[str] = []
    if was_downsampled:
        warning_parts.append(
            f"Control points were downsampled to {len(control_points)} for performance. Increase max control points for more detail."
        )
    if soft_fallback_count:
        warning_parts.append(
            f"{soft_fallback_count} control point(s) had no candidate within max snap distance, so the nearest node was used anyway."
        )
    if not closed:
        warning_parts.append(
            "A fully closed network loop was not found, so the app returned the best connected open sequence."
        )
    if snapped_projected.geom_type == "MultiLineString":
        warning_parts.append(
            "Output is a MultiLineString. This can happen when the chosen loop reuses edges or the network has tiny disconnected geometry pieces."
        )

    return SnapResult(
        snapped_line_geojson=mapping(snapped_wgs84),
        original_polygon_geojson=mapping(polygon),
        query_area_geojson=mapping(query_polygon_wgs84),
        network_nodes_count=int(graph_route.number_of_nodes()),
        network_edges_count=int(graph_route.number_of_edges()),
        control_points_count=int(len(control_points)),
        mean_snap_distance_m=float(snap_sum / max(1, len(selected_nodes))),
        max_snap_distance_m=float(max(all_candidate_distances) if all_candidate_distances else 0.0),
        output_length_m=float(route_length),
        route_piece_count=int(route_piece_count),
        closed_loop=bool(closed),
        warning=" ".join(warning_parts) if warning_parts else None,
    )
