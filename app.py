from __future__ import annotations

import json
from typing import Any

import folium
import streamlit as st
from folium.plugins import Draw
from shapely.geometry import shape
from streamlit_folium import st_folium

from snapper import snap_polygon_to_nearest_network


st.set_page_config(
    page_title="Polygon Road/Rail Snapper",
    page_icon="🗺️",
    layout="wide",
)

st.title("🗺️ Polygon Road/Rail Snapper")
st.caption(
    "Draw a polygon, then snap its boundary inward/outward to nearby OpenStreetMap roads and rail lines."
)


@st.cache_data(show_spinner=False)
def snap_cached(
    drawn_geojson_string: str,
    target: str,
    search_buffer_m: float,
    sample_spacing_m: float,
    max_snap_distance_m: float,
    candidate_count: int,
    switch_penalty_m: float,
) -> dict[str, Any]:
    """Cache expensive OSM calls and snapping results."""
    drawn_geojson = json.loads(drawn_geojson_string)
    result = snap_polygon_to_nearest_network(
        drawn_geojson=drawn_geojson,
        target=target,  # type: ignore[arg-type]
        search_buffer_m=search_buffer_m,
        sample_spacing_m=sample_spacing_m,
        max_snap_distance_m=max_snap_distance_m,
        candidate_count=candidate_count,
        switch_penalty_m=switch_penalty_m,
    )
    return result.__dict__


with st.sidebar:
    st.header("Snap settings")

    target_label = st.selectbox(
        "Snap to",
        ["Roads + rail", "Roads only", "Rail only"],
        index=0,
    )
    target_map = {
        "Roads + rail": "roads_and_rails",
        "Roads only": "roads",
        "Rail only": "rails",
    }

    search_buffer_m = st.slider(
        "Search buffer around polygon, meters",
        min_value=50,
        max_value=1000,
        value=200,
        step=50,
        help="Larger values find roads/rails farther from the drawn polygon but make OSM queries slower.",
    )

    sample_spacing_m = st.slider(
        "Boundary sample spacing, meters",
        min_value=5,
        max_value=100,
        value=15,
        step=5,
        help="Lower values sample the polygon more closely. 10-20m usually works well in cities.",
    )

    max_snap_distance_m = st.slider(
        "Max snap distance, meters",
        min_value=10,
        max_value=500,
        value=120,
        step=10,
        help="Boundary samples farther than this from a road/rail line are ignored.",
    )

    candidate_count = st.slider(
        "Nearby candidates per sample",
        min_value=1,
        max_value=10,
        value=5,
        step=1,
        help="Higher values let the algorithm choose a smoother nearby road instead of always the single nearest one.",
    )

    switch_penalty_m = st.slider(
        "Road-switch penalty, meters",
        min_value=0,
        max_value=150,
        value=35,
        step=5,
        help="Higher values reduce zig-zags by discouraging jumps between parallel roads.",
    )

    st.divider()
    st.markdown(
        "**Important:** this V2 output uses actual OSM road/rail linework. It avoids fake diagonal connectors."
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
        st.info("Draw one polygon on the map. The snap controls will appear here after the drawing is detected.")
        st.stop()

    drawn = drawings[-1]
    drawn_geojson_string = json.dumps(drawn, sort_keys=True)

    with st.expander("Raw drawn GeoJSON", expanded=False):
        st.json(drawn)

    if st.button("Snap polygon to nearest network", type="primary", use_container_width=True):
        with st.spinner("Querying OpenStreetMap and snapping polygon boundary..."):
            try:
                result = snap_cached(
                    drawn_geojson_string=drawn_geojson_string,
                    target=target_map[target_label],
                    search_buffer_m=float(search_buffer_m),
                    sample_spacing_m=float(sample_spacing_m),
                    max_snap_distance_m=float(max_snap_distance_m),
                    candidate_count=int(candidate_count),
                    switch_penalty_m=float(switch_penalty_m),
                )
                st.session_state["snap_result"] = result
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
                st.stop()

    result = st.session_state.get("snap_result")

    if result:
        if result.get("warning"):
            st.warning(result["warning"])

        st.metric("Network line features found", result["network_features_count"])
        st.metric("Boundary sample points", result["sampled_points_count"])
        st.metric("Road-aligned pieces", result["road_aligned_segments_count"])
        st.metric("Skipped fake transitions", result["skipped_transition_count"])
        st.metric("Mean snap distance", f"{result['mean_snap_distance_m']:.1f} m")
        st.metric("Max nearest distance", f"{result['max_snap_distance_m']:.1f} m")

        snapped_feature = {
            "type": "Feature",
            "properties": {
                "name": "snapped_boundary_v2",
                "target": target_label,
                "sample_spacing_m": sample_spacing_m,
                "search_buffer_m": search_buffer_m,
                "max_snap_distance_m": max_snap_distance_m,
                "candidate_count": candidate_count,
                "switch_penalty_m": switch_penalty_m,
                "note": "V2 uses actual road/rail linework and intentionally avoids fake diagonal connectors.",
            },
            "geometry": result["snapped_line_geojson"],
        }

        feature_collection = {
            "type": "FeatureCollection",
            "features": [snapped_feature],
        }

        st.download_button(
            "Download snapped GeoJSON",
            data=json.dumps(feature_collection, indent=2),
            file_name="snapped_polygon_boundary_v2.geojson",
            mime="application/geo+json",
            use_container_width=True,
        )

        st.subheader("3. Preview snapped linework")

        original_geom = shape(drawn["geometry"])
        minx, miny, maxx, maxy = original_geom.bounds
        preview_center = [(miny + maxy) / 2, (minx + maxx) / 2]

        preview = folium.Map(location=preview_center, zoom_start=16, tiles="OpenStreetMap")

        folium.GeoJson(
            drawn,
            name="Original polygon",
            style_function=lambda _: {"color": "blue", "weight": 2, "fillOpacity": 0.05},
        ).add_to(preview)

        folium.GeoJson(
            snapped_feature,
            name="Snapped road/rail linework",
            style_function=lambda _: {"color": "red", "weight": 5},
        ).add_to(preview)

        folium.LayerControl().add_to(preview)
        preview.fit_bounds([[miny, minx], [maxy, maxx]])

        st_folium(preview, height=420, width=None, key="preview_map")

        with st.expander("Snapped GeoJSON", expanded=False):
            st.json(feature_collection)
    else:
        st.info("Click the snap button after drawing your polygon.")
