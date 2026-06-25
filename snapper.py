from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
from shapely.geometry import LineString, MultiLineString, Point, Polygon, mapping, shape
from shapely.ops import linemerge, polygonize, unary_union

SnapTarget = Literal["roads", "rails", "roads_and_rails"]
RoadTier = Literal["main", "public", "all_drivable"]

ROAD_TIERS: dict[RoadTier, set[str]] = {
    # Use this when you want to ignore small side streets as much as possible.
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
    # Recommended default. Keeps normal public streets but excludes footways, pedestrian paths,
    # tracks, crossings, and small service roads such as driveways and car-park aisles.
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
    # Use only when the snapped polygon needs private/service access roads too.
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
    selected_cells_count: int
    output_area_m2: float
    output_perimeter_m: float
    coordinate_count: int
    closed_loop: bool
    warning: str | None = None


def _ensure_polygon_from_geojson(geojson: dict[str, Any]) -> Polygon:
    """Return a valid Polygon from a GeoJSON Feature or Geometry."""
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
    """Create a WGS84 query buffer using a local metric projection."""
    polygon_wgs84 = _polygon_gdf(polygon)
    polygon_projected = ox.projection.project_gdf(polygon_wgs84)
    buffered_projected = polygon_projected.geometry.iloc[0].buffer(float(buffer_m))
    buffered_wgs84 = gpd.GeoDataFrame(geometry=[buffered_projected], crs=polygon_projected.crs).to_crs(
        "EPSG:4326"
    )
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
    """Convert to undirected because this is polygon linework, not human navigation."""
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
    """
    Remove dangling branches so the output behaves like a polygon boundary, not a route.

    This is the main fix for red lines that poke out and go nowhere. We repeatedly remove
    degree-0 and degree-1 nodes. What remains is the cyclic/core part of the network.
    """
    pruned = graph.copy()

    while True:
        leaves = [node for node, degree in pruned.degree() if degree <= 1]
        if not leaves:
            break
        pruned.remove_nodes_from(leaves)

    # If pruning destroyed the graph, fall back to the original network instead of failing.
    if pruned.number_of_edges() == 0:
        return graph
    return pruned


def _line_geometries_from_edges(edges: gpd.GeoDataFrame) -> list[LineString]:
    lines: list[LineString] = []
    for geom in edges.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "LineString":
            lines.append(geom)
        elif geom.geom_type == "MultiLineString":
            lines.extend([part for part in geom.geoms if not part.is_empty and part.length > 0])
    return lines


def _largest_polygon_component(geom: Any) -> tuple[Any, bool]:
    if geom.geom_type == "MultiPolygon":
        parts = [part for part in geom.geoms if not part.is_empty]
        if not parts:
            return geom, False
        return max(parts, key=lambda part: part.area), True
    return geom, False


def _count_coordinates(geom: Any) -> int:
    if geom.is_empty:
        return 0
    if geom.geom_type == "Point":
        return 1
    if geom.geom_type in {"LineString", "LinearRing"}:
        return len(geom.coords)
    if geom.geom_type == "Polygon":
        return len(geom.exterior.coords) + sum(len(ring.coords) for ring in geom.interiors)
    if hasattr(geom, "geoms"):
        return sum(_count_coordinates(part) for part in geom.geoms)
    return 0


def _build_cell_polygon(
    edges_projected: gpd.GeoDataFrame,
    drawn_polygon_projected: Polygon,
    min_cell_overlap: float,
    min_cell_area_m2: float,
    simplify_tolerance_m: float,
    keep_largest_component: bool,
) -> tuple[Any, dict[str, Any]]:
    """
    Convert road/rail linework into closed cells, select cells that overlap the drawn polygon,
    then dissolve them into one snapped polygon.

    This treats the road network like a set of polygon boundaries. It does not compute a
    pedestrian/driving path and it does not connect points with diagonal shortcuts.
    """
    lines = _line_geometries_from_edges(edges_projected)
    if not lines:
        raise ValueError("No usable road/rail line geometry was found after filtering.")

    noded_linework = unary_union(lines)
    cells = [cell for cell in polygonize(noded_linework) if cell.area >= float(min_cell_area_m2)]

    if not cells:
        raise ValueError(
            "The selected road/rail network did not form any closed cells. Try Public streets instead of Main roads only, "
            "or increase the search buffer."
        )

    selected_cells = []
    drawn_area = max(drawn_polygon_projected.area, 1.0)

    for cell in cells:
        if not cell.intersects(drawn_polygon_projected):
            continue

        intersection_area = cell.intersection(drawn_polygon_projected).area
        cell_overlap = intersection_area / max(cell.area, 1.0)
        drawn_overlap = intersection_area / drawn_area
        cell_center_inside = cell.representative_point().within(drawn_polygon_projected)

        if cell_center_inside or cell_overlap >= float(min_cell_overlap) or drawn_overlap >= 0.01:
            selected_cells.append(cell)

    if not selected_cells:
        # Last-resort fallback: choose the road cell whose boundary is nearest to the drawn polygon boundary.
        selected_cells = [
            min(cells, key=lambda cell: cell.boundary.distance(drawn_polygon_projected.boundary))
        ]

    dissolved = unary_union(selected_cells)

    reduced_to_largest = False
    if keep_largest_component:
        dissolved, reduced_to_largest = _largest_polygon_component(dissolved)

    if simplify_tolerance_m > 0:
        dissolved = dissolved.simplify(float(simplify_tolerance_m), preserve_topology=True)

    if not dissolved.is_valid:
        dissolved = dissolved.buffer(0)

    if dissolved.is_empty:
        raise ValueError("The snapped polygon became empty after dissolve/simplification. Try a lower simplify tolerance.")

    meta = {
        "candidate_cells_count": len(cells),
        "selected_cells_count": len(selected_cells),
        "reduced_to_largest": reduced_to_largest,
    }
    return dissolved, meta


def snap_polygon_to_road_rail_polygon(
    drawn_geojson: dict[str, Any],
    target: SnapTarget = "roads",
    road_tier: RoadTier = "public",
    search_buffer_m: float = 250,
    min_cell_overlap: float = 0.20,
    min_cell_area_m2: float = 500,
    simplify_tolerance_m: float = 8,
    keep_largest_component: bool = True,
    prune_dead_ends: bool = True,
) -> SnapResult:
    """
    Snap a drawn polygon to nearby road/rail linework by creating closed road-network cells.

    V4 behavior:
    - Direction does not matter: the graph is undirected.
    - Pedestrian paths/crossings are excluded by default.
    - Service roads are excluded by default.
    - Dead-end branches are pruned so the red output does not poke out into nowhere.
    - The output is a closed polygon/multipolygon boundary, not a human route.
    - Output coordinates can be simplified so the result has fewer points.
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
        raise ValueError(
            "No matching roads/rails were found near this polygon. Try increasing the search buffer or lowering the road tier."
        )

    graph = _filter_graph_edges(graph, target=target, road_tier=road_tier)
    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        raise ValueError(
            "All downloaded network edges were filtered out. Try Public streets or All drivable roads."
        )

    graph_projected = ox.project_graph(graph)
    graph_undirected = _to_undirected_graph(graph_projected)
    if prune_dead_ends:
        graph_undirected = _prune_dead_ends(graph_undirected)

    nodes_projected, edges_projected = ox.graph_to_gdfs(graph_undirected, nodes=True, edges=True)
    polygon_projected = _polygon_gdf(polygon).to_crs(nodes_projected.crs).geometry.iloc[0]

    snapped_projected, meta = _build_cell_polygon(
        edges_projected=edges_projected,
        drawn_polygon_projected=polygon_projected,
        min_cell_overlap=min_cell_overlap,
        min_cell_area_m2=min_cell_area_m2,
        simplify_tolerance_m=simplify_tolerance_m,
        keep_largest_component=keep_largest_component,
    )

    boundary_projected = snapped_projected.boundary

    snapped_wgs84 = gpd.GeoDataFrame(geometry=[snapped_projected], crs=nodes_projected.crs).to_crs(
        "EPSG:4326"
    ).geometry.iloc[0]
    boundary_wgs84 = gpd.GeoDataFrame(geometry=[boundary_projected], crs=nodes_projected.crs).to_crs(
        "EPSG:4326"
    ).geometry.iloc[0]

    warning_parts: list[str] = []
    if meta.get("reduced_to_largest"):
        warning_parts.append(
            "The selected road cells formed multiple disconnected polygons, so only the largest closed component was kept."
        )
    if snapped_projected.geom_type == "MultiPolygon":
        warning_parts.append("Output is a MultiPolygon. Enable 'keep largest component' for one loop only.")
    if target != "roads":
        warning_parts.append(
            "Road+rail or rail-only snapping can create unusual cells where rail lines cross roads. Roads only is usually cleaner."
        )

    return SnapResult(
        snapped_geojson=mapping(snapped_wgs84),
        snapped_boundary_geojson=mapping(boundary_wgs84),
        original_polygon_geojson=mapping(polygon),
        query_area_geojson=mapping(query_polygon_wgs84),
        algorithm="cell_polygonizer_v4",
        network_nodes_count=int(graph_undirected.number_of_nodes()),
        network_edges_count=int(graph_undirected.number_of_edges()),
        candidate_cells_count=int(meta["candidate_cells_count"]),
        selected_cells_count=int(meta["selected_cells_count"]),
        output_area_m2=float(snapped_projected.area),
        output_perimeter_m=float(snapped_projected.length),
        coordinate_count=int(_count_coordinates(snapped_projected)),
        closed_loop=snapped_projected.geom_type in {"Polygon", "MultiPolygon"},
        warning=" ".join(warning_parts) if warning_parts else None,
    )
