from __future__ import annotations

import json
from typing import Any

import folium
import streamlit as st
from folium.plugins import Draw
from shapely.geometry import shape
from streamlit_folium import st_folium

from snapper import snap_polygon_to_road_rail_polygon


st.set_page_config(
    page_title="Road/Rail Polygon Snapper V4",
    page_icon="🗺️",
    layout="wide",
)

st.title("🗺️ Road/Rail Polygon Snapper V4")
st.caption(
    "Draw a polygon. The app builds closed road-network cells and returns a polygon boundary snapped to roads/rails. "
    "This version is for polygon geometry, not human routing."
)


@st.cache_data(show_spinner=False)
def snap_cached(
    drawn_geojson_string: str,
    target: str,
    road_tier: str,
    search_buffer_m: float,
    min_cell_overlap: float,
    min_cell_area_m2: float,
    simplify_tolerance_m: float,
    keep_largest_component: bool,
    prune_dead_ends: bool,
) -> dict[str, Any]:
    drawn_geojson = json.loads(drawn_geojson_string)
    result = snap_polygon_to_road_rail_polygon(
        drawn_geojson=drawn_geojson,
        target=target,  # type: ignore[arg-type]
        road_tier=road_tier,  # type: ignore[arg-type]
        search_buffer_m=search_buffer_m,
        min_cell_overlap=min_cell_overlap,
        min_cell_area_m2=min_cell_area_m2,
        simplify_tolerance_m=simplify_tolerance_m,
        keep_largest_component=keep_largest_component,
        prune_dead_ends=prune_dead_ends,
    )
    return result.__dict__


with st.sidebar:
    st.header("Snap settings")

    target_label = st.selectbox(
        "Snap to",
        ["Roads only", "Roads + rail", "Rail only"],
        index=0,
        help="Roads only is usually cleanest for polygon boundaries. Rail can be useful when rails are intended as boundaries.",
    )
    target_map = {
        "Roads only": "roads",
        "Roads + rail": "roads_and_rails",
        "Rail only": "rails",
    }

    road_tier_label = st.selectbox(
        "Road tier",
        [
            "Public streets, no service roads or paths",
            "Main roads only",
            "All drivable roads incl. service roads",
        ],
        index=0,
        help=(
            "Use Main roads only to ignore more small side roads. "
            "The default excludes footways, pedestrian paths, crossings, tracks, cycleways, and service roads."
        ),
    )
    road_tier_map = {
        "Main roads only": "main",
        "Public streets, no service roads or paths": "public",
        "All drivable roads incl. service roads": "all_drivable",
    }

    search_buffer_m = st.slider(
        "Search buffer around polygon, meters",
        min_value=50,
        max_value=1500,
        value=300,
        step=50,
        help="Larger values give the polygonizer more road cells around the drawn polygon, but queries become slower.",
    )

    min_cell_overlap_pct = st.slider(
        "Cell inclusion threshold",
        min_value=5,
        max_value=80,
        value=20,
        step=5,
        help=(
            "Lower includes more nearby road cells, often expanding outward. "
            "Higher is stricter and may shrink inward. Start at 20%."
        ),
    )

    min_cell_area_m2 = st.slider(
        "Ignore tiny closed cells below, m²",
        min_value=0,
        max_value=10000,
        value=500,
        step=250,
        help="Raises the floor for tiny polygons created by small traffic islands or mapping artifacts.",
    )

    simplify_tolerance_m = st.slider(
        "Simplify output tolerance, meters",
        min_value=0,
        max_value=50,
        value=8,
        step=1,
        help="Higher values produce fewer coordinate points. Too high can oversimplify corners.",
    )

    keep_largest_component = st.checkbox(
        "Keep only largest closed component",
        value=True,
        help="Recommended. Prevents disconnected little road cells from becoming extra polygons.",
    )

    prune_dead_ends = st.checkbox(
        "Prune dead-end branches before polygonizing",
        value=True,
        help="Recommended. Removes cul-de-sacs and dangling linework so the red result does not poke out into nowhere.",
    )

    st.divider()
    st.markdown(
        "**V4 behavior:** no one-way rules, no pedestrian routing, no diagonal shortcuts. "
        "It polygonizes road/rail linework and returns a closed snapped boundary."
    )


left, right = st.columns([0.62, 0.38], gap="large")

with left:
    st.subheader("1. Draw polygon")

    m = folium.Map(
        location=[1.3521, 103.8198],
        zoom_start=12,
        tiles="OpenStreetMap",
        control_scale=True,
    )

    Draw(
        export=False,
        draw_options={
            "polyline": False,
            "rectangle": False,
            "circle": False,
            "circlemarker": False,
            "marker": False,
            "polygon": {
                "allowIntersection": False,
                "showArea": True,
                "shapeOptions": {"color": "#3388ff", "weight": 3},
            },
        },
        edit_options={"edit": True, "remove": True},
    ).add_to(m)

    map_data = st_folium(
        m,
        height=650,
        width=None,
        returned_objects=["all_drawings", "last_active_drawing"],
        key="draw_map",
    )

with right:
    st.subheader("2. Snap result")

    drawings = map_data.get("all_drawings") if map_data else None

    if not drawings:
        st.info("Draw one polygon on the map. Then click the snap button here.")
        st.stop()

    drawn = drawings[-1]
    drawn_geojson_string = json.dumps(drawn, sort_keys=True)

    with st.expander("Raw drawn GeoJSON", expanded=False):
        st.json(drawn)

    if st.button("Snap to closed road/rail polygon", type="primary", use_container_width=True):
        with st.spinner("Querying OpenStreetMap and building closed road cells..."):
            try:
                result = snap_cached(
                    drawn_geojson_string=drawn_geojson_string,
                    target=target_map[target_label],
                    road_tier=road_tier_map[road_tier_label],
                    search_buffer_m=float(search_buffer_m),
                    min_cell_overlap=float(min_cell_overlap_pct / 100),
                    min_cell_area_m2=float(min_cell_area_m2),
                    simplify_tolerance_m=float(simplify_tolerance_m),
                    keep_largest_component=bool(keep_largest_component),
                    prune_dead_ends=bool(prune_dead_ends),
                )
                st.session_state["snap_result_v4"] = result
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
                st.stop()

    result = st.session_state.get("snap_result_v4")

    if result:
        if result.get("warning"):
            st.warning(result["warning"])

        closed_text = "Yes" if result["closed_loop"] else "No"
        st.metric("Closed polygon", closed_text)
        st.metric("Algorithm", result["algorithm"])
        st.metric("Network nodes", result["network_nodes_count"])
        st.metric("Network edges", result["network_edges_count"])
        st.metric("Candidate road cells", result["candidate_cells_count"])
        st.metric("Selected road cells", result["selected_cells_count"])
        st.metric("Coordinate points", result["coordinate_count"])
        st.metric("Output area", f"{result['output_area_m2']:.0f} m²")
        st.metric("Output perimeter", f"{result['output_perimeter_m']:.0f} m")

        snapped_polygon_feature = {
            "type": "Feature",
            "properties": {
                "name": "snapped_polygon_v4",
                "target": target_label,
                "road_tier": road_tier_label,
                "search_buffer_m": search_buffer_m,
                "min_cell_overlap_pct": min_cell_overlap_pct,
                "min_cell_area_m2": min_cell_area_m2,
                "simplify_tolerance_m": simplify_tolerance_m,
                "note": "V4 polygonizes road/rail cells. It is not a pedestrian/driving route.",
            },
            "geometry": result["snapped_geojson"],
        }

        snapped_boundary_feature = {
            "type": "Feature",
            "properties": {
                "name": "snapped_boundary_v4",
                "target": target_label,
                "road_tier": road_tier_label,
            },
            "geometry": result["snapped_boundary_geojson"],
        }

        feature_collection = {
            "type": "FeatureCollection",
            "features": [snapped_polygon_feature, snapped_boundary_feature],
        }

        st.download_button(
            "Download snapped GeoJSON",
            data=json.dumps(feature_collection, indent=2),
            file_name="snapped_polygon_v4.geojson",
            mime="application/geo+json",
            use_container_width=True,
        )

        st.subheader("3. Preview")

        original_geom = shape(drawn["geometry"])
        minx, miny, maxx, maxy = original_geom.bounds
        preview_center = [(miny + maxy) / 2, (minx + maxx) / 2]

        preview = folium.Map(location=preview_center, zoom_start=16, tiles="OpenStreetMap")

        folium.GeoJson(
            drawn,
            name="Original drawn polygon",
            style_function=lambda _: {"color": "blue", "weight": 2, "fillOpacity": 0.04},
        ).add_to(preview)

        folium.GeoJson(
            snapped_polygon_feature,
            name="Snapped polygon fill",
            style_function=lambda _: {
                "color": "red",
                "weight": 4,
                "fillColor": "red",
                "fillOpacity": 0.08,
            },
        ).add_to(preview)

        folium.GeoJson(
            snapped_boundary_feature,
            name="Snapped polygon boundary",
            style_function=lambda _: {"color": "red", "weight": 5},
        ).add_to(preview)

        folium.LayerControl().add_to(preview)
        preview.fit_bounds([[miny, minx], [maxy, maxx]])

        st_folium(preview, height=420, width=None, key="preview_map")

        with st.expander("Snapped GeoJSON", expanded=False):
            st.json(feature_collection)
    else:
        st.info("Click the snap button after drawing your polygon.")
