from __future__ import annotations

import json
from typing import Any

import folium
import streamlit as st
from folium.plugins import Draw
from shapely.geometry import shape
from streamlit_folium import st_folium

from snapper import snap_polygon_to_closed_network_loop


st.set_page_config(
    page_title="Closed Loop Road Snapper",
    page_icon="🗺️",
    layout="wide",
)

st.title("🗺️ Closed Loop Road/Rail Snapper")
st.caption(
    "Draw a polygon, then fit it to a nearby connected OpenStreetMap road/rail loop. "
    "V3 prioritizes closure instead of snapping each point independently."
)


@st.cache_data(show_spinner=False)
def snap_cached(
    drawn_geojson_string: str,
    target: str,
    search_buffer_m: float,
    control_spacing_m: float,
    max_snap_distance_m: float,
    candidate_count: int,
    boundary_closeness_weight: float,
    max_control_points: int,
) -> dict[str, Any]:
    drawn_geojson = json.loads(drawn_geojson_string)
    result = snap_polygon_to_closed_network_loop(
        drawn_geojson=drawn_geojson,
        target=target,  # type: ignore[arg-type]
        search_buffer_m=search_buffer_m,
        control_spacing_m=control_spacing_m,
        max_snap_distance_m=max_snap_distance_m,
        candidate_count=candidate_count,
        boundary_closeness_weight=boundary_closeness_weight,
        max_control_points=max_control_points,
    )
    return result.__dict__


with st.sidebar:
    st.header("Snap settings")

    target_label = st.selectbox(
        "Snap to",
        ["Roads only", "Roads + rail", "Rail only"],
        index=0,
        help="Roads only is usually best for closed loops. Roads + rail can be fragmented if rail lines do not connect to roads.",
    )
    target_map = {
        "Roads only": "roads",
        "Roads + rail": "roads_and_rails",
        "Rail only": "rails",
    }

    search_buffer_m = st.slider(
        "Search buffer around polygon, meters",
        min_value=50,
        max_value=1500,
        value=300,
        step=50,
        help="Larger values give the loop algorithm more possible roads to close the shape, but queries become slower.",
    )

    control_spacing_m = st.slider(
        "Loop control-point spacing, meters",
        min_value=20,
        max_value=200,
        value=60,
        step=10,
        help="Lower values preserve the drawn shape more closely. Higher values are faster and smoother.",
    )

    max_snap_distance_m = st.slider(
        "Max candidate distance, meters",
        min_value=20,
        max_value=600,
        value=180,
        step=20,
        help="Candidate road nodes farther than this are ignored unless no candidate exists for a control point.",
    )

    candidate_count = st.slider(
        "Nearby candidates per control point",
        min_value=2,
        max_value=10,
        value=5,
        step=1,
        help="Higher values give the algorithm more options for finding a closed loop.",
    )

    boundary_closeness_weight = st.slider(
        "Boundary closeness weight",
        min_value=1.0,
        max_value=20.0,
        value=6.0,
        step=0.5,
        help="Higher values keep the result closer to the drawn polygon. Lower values prioritize simpler/shorter connected loops.",
    )

    max_control_points = st.slider(
        "Max control points",
        min_value=20,
        max_value=120,
        value=70,
        step=10,
        help="Safety limit for Streamlit Cloud. Increase for large polygons if performance is acceptable.",
    )

    st.divider()
    st.markdown(
        "**V3 behavior:** chooses roads that can connect back to the start. "
        "It may move slightly inward/outward or choose a slightly farther road if that helps form a closed loop."
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

    if st.button("Snap to closed road/rail loop", type="primary", use_container_width=True):
        with st.spinner("Querying OpenStreetMap and solving the closed loop..."):
            try:
                result = snap_cached(
                    drawn_geojson_string=drawn_geojson_string,
                    target=target_map[target_label],
                    search_buffer_m=float(search_buffer_m),
                    control_spacing_m=float(control_spacing_m),
                    max_snap_distance_m=float(max_snap_distance_m),
                    candidate_count=int(candidate_count),
                    boundary_closeness_weight=float(boundary_closeness_weight),
                    max_control_points=int(max_control_points),
                )
                st.session_state["snap_result_v3"] = result
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
                st.stop()

    result = st.session_state.get("snap_result_v3")

    if result:
        if result.get("warning"):
            st.warning(result["warning"])

        closed_text = "Yes" if result["closed_loop"] else "No"
        st.metric("Closed loop found", closed_text)
        st.metric("Network nodes", result["network_nodes_count"])
        st.metric("Network edges", result["network_edges_count"])
        st.metric("Control points", result["control_points_count"])
        st.metric("Route pieces", result["route_piece_count"])
        st.metric("Mean snap distance", f"{result['mean_snap_distance_m']:.1f} m")
        st.metric("Output length", f"{result['output_length_m']:.0f} m")

        snapped_feature = {
            "type": "Feature",
            "properties": {
                "name": "snapped_boundary_v3_closed_loop",
                "target": target_label,
                "search_buffer_m": search_buffer_m,
                "control_spacing_m": control_spacing_m,
                "max_snap_distance_m": max_snap_distance_m,
                "candidate_count": candidate_count,
                "boundary_closeness_weight": boundary_closeness_weight,
                "closed_loop": result["closed_loop"],
                "note": "V3 prioritizes a connected closed loop using real OSM network linework.",
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
            file_name="snapped_closed_loop_v3.geojson",
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
            name="Original polygon",
            style_function=lambda _: {"color": "blue", "weight": 2, "fillOpacity": 0.05},
        ).add_to(preview)

        folium.GeoJson(
            snapped_feature,
            name="Snapped closed loop",
            style_function=lambda _: {"color": "red", "weight": 5},
        ).add_to(preview)

        folium.LayerControl().add_to(preview)
        preview.fit_bounds([[miny, minx], [maxy, maxx]])

        st_folium(preview, height=420, width=None, key="preview_map")

        with st.expander("Snapped GeoJSON", expanded=False):
            st.json(feature_collection)
    else:
        st.info("Click the snap button after drawing your polygon.")
