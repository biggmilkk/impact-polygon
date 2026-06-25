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
    page_title="Road/Rail Polygon Snapper V5",
    page_icon="🗺️",
    layout="wide",
)

st.title("🗺️ Road/Rail Polygon Snapper V5")
st.caption(
    "Draw a polygon. V5 chooses a best-fitting closed road/rail cell polygon, not a pedestrian route. "
    "Direction, one-way streets, and crossings are ignored."
)


@st.cache_data(show_spinner=False)
def snap_cached(
    drawn_geojson_string: str,
    target: str,
    road_tier: str,
    fit_mode: str,
    search_buffer_m: float,
    min_cell_area_m2: float,
    max_cell_area_multiple: float,
    min_cell_inside_ratio: float,
    max_cell_outside_ratio: float,
    simplify_tolerance_m: float,
    prune_dead_ends: bool,
    max_refinement_iterations: int,
) -> dict[str, Any]:
    drawn_geojson = json.loads(drawn_geojson_string)
    result = snap_polygon_to_road_rail_polygon(
        drawn_geojson=drawn_geojson,
        target=target,  # type: ignore[arg-type]
        road_tier=road_tier,  # type: ignore[arg-type]
        fit_mode=fit_mode,  # type: ignore[arg-type]
        search_buffer_m=search_buffer_m,
        min_cell_area_m2=min_cell_area_m2,
        max_cell_area_multiple=max_cell_area_multiple,
        min_cell_inside_ratio=min_cell_inside_ratio,
        max_cell_outside_ratio=max_cell_outside_ratio,
        simplify_tolerance_m=simplify_tolerance_m,
        prune_dead_ends=prune_dead_ends,
        max_refinement_iterations=max_refinement_iterations,
    )
    return result.__dict__


with st.sidebar:
    st.header("Snap settings")

    target_label = st.selectbox(
        "Snap to",
        ["Roads only", "Roads + rail", "Rail only"],
        index=0,
        help="Roads only is usually cleanest. Add rail only when rails should act as polygon boundaries.",
    )
    target_map = {
        "Roads only": "roads",
        "Roads + rail": "roads_and_rails",
        "Rail only": "rails",
    }

    road_tier_label = st.selectbox(
        "Road tier",
        [
            "Main roads only",
            "Arterial roads only",
            "Public streets, no service roads or paths",
            "All drivable roads incl. service roads",
        ],
        index=0,
        help=(
            "Main roads only is the cleaner default. Public streets can close smaller polygons but may include too many side streets. "
            "Footways, pedestrian paths, crossings, cycleways, tracks, and steps are always excluded."
        ),
    )
    road_tier_map = {
        "Arterial roads only": "arterial",
        "Main roads only": "main",
        "Public streets, no service roads or paths": "public",
        "All drivable roads incl. service roads": "all_drivable",
    }

    fit_mode_label = st.selectbox(
        "Fit behavior",
        ["Balanced inward/outward", "Tight / avoid outside bulges", "Cover input polygon more"],
        index=1,
        help=(
            "Tight is best when red output expands too far outside the blue polygon. "
            "Cover is best when the output shrinks too much."
        ),
    )
    fit_mode_map = {
        "Balanced inward/outward": "balanced",
        "Tight / avoid outside bulges": "tight",
        "Cover input polygon more": "cover",
    }

    search_buffer_m = st.slider(
        "Search buffer around polygon, meters",
        min_value=50,
        max_value=1500,
        value=350,
        step=50,
        help="Larger gives more possible roads/cells. Too large can add unwanted alternatives and slow the query.",
    )

    min_cell_area_m2 = st.slider(
        "Ignore tiny closed cells below, m²",
        min_value=0,
        max_value=20000,
        value=750,
        step=250,
        help="Raises the floor for tiny traffic islands or mapping artifacts.",
    )

    max_cell_area_multiple = st.slider(
        "Ignore very large cells above input-area multiple",
        min_value=0.5,
        max_value=8.0,
        value=2.0,
        step=0.5,
        help="Lower values reject big exterior cells that cause large red bulges. Try 1.5 to 2.5 first.",
    )

    min_cell_inside_pct = st.slider(
        "Minimum cell overlap with input polygon",
        min_value=5,
        max_value=90,
        value=35,
        step=5,
        help="Higher is stricter and avoids outside bulges. Lower helps when roads sit just outside the drawn polygon.",
    )

    max_cell_outside_pct = st.slider(
        "Maximum outside share for center-inside cells",
        min_value=10,
        max_value=95,
        value=65,
        step=5,
        help="Lower values reject cells that stick far outside the blue polygon.",
    )

    simplify_tolerance_m = st.slider(
        "Simplify output tolerance, meters",
        min_value=0,
        max_value=80,
        value=12,
        step=1,
        help="Higher values produce fewer points. Too high can cut corners.",
    )

    prune_dead_ends = st.checkbox(
        "Prune dead-end branches before polygonizing",
        value=True,
        help="Recommended. Removes dangling linework before closed cells are built.",
    )

    max_refinement_iterations = st.slider(
        "Best-fit refinement iterations",
        min_value=0,
        max_value=80,
        value=30,
        step=5,
        help="Higher lets the app remove/add more whole cells to improve the final shape, but can be slower.",
    )

    st.divider()
    st.markdown(
        "**V5 behavior:** selects whole closed road cells and scores the final polygon against the blue input. "
        "It keeps the best-fitting closed component, not the largest one."
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

    if st.button("Snap to best-fitting closed road/rail polygon", type="primary", use_container_width=True):
        with st.spinner("Querying OpenStreetMap and fitting closed road cells..."):
            try:
                result = snap_cached(
                    drawn_geojson_string=drawn_geojson_string,
                    target=target_map[target_label],
                    road_tier=road_tier_map[road_tier_label],
                    fit_mode=fit_mode_map[fit_mode_label],
                    search_buffer_m=float(search_buffer_m),
                    min_cell_area_m2=float(min_cell_area_m2),
                    max_cell_area_multiple=float(max_cell_area_multiple),
                    min_cell_inside_ratio=float(min_cell_inside_pct / 100),
                    max_cell_outside_ratio=float(max_cell_outside_pct / 100),
                    simplify_tolerance_m=float(simplify_tolerance_m),
                    prune_dead_ends=bool(prune_dead_ends),
                    max_refinement_iterations=int(max_refinement_iterations),
                )
                st.session_state["snap_result_v5"] = result
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
                st.stop()

    result = st.session_state.get("snap_result_v5")

    if result:
        if result.get("warning"):
            st.warning(result["warning"])

        closed_text = "Yes" if result["closed_loop"] else "No"
        st.metric("Closed polygon", closed_text)
        st.metric("Algorithm", result["algorithm"])
        st.metric("Fit score", f"{result['fit_score']:.3f}")
        st.metric("Coverage of input", f"{result['coverage_ratio'] * 100:.0f}%")
        st.metric("Output outside input", f"{result['outside_ratio'] * 100:.0f}%")
        st.metric("Input missing from output", f"{result['missing_ratio'] * 100:.0f}%")
        st.metric("Candidate road cells", result["candidate_cells_count"])
        st.metric("Initially selected cells", result["initially_selected_cells_count"])
        st.metric("Final selected cells", result["final_selected_cells_count"])
        st.metric("Coordinate points", result["coordinate_count"])
        st.metric("Output area", f"{result['output_area_m2']:.0f} m²")
        st.metric("Output perimeter", f"{result['output_perimeter_m']:.0f} m")

        snapped_polygon_feature = {
            "type": "Feature",
            "properties": {
                "name": "snapped_polygon_v5",
                "target": target_label,
                "road_tier": road_tier_label,
                "fit_mode": fit_mode_label,
                "search_buffer_m": search_buffer_m,
                "min_cell_area_m2": min_cell_area_m2,
                "max_cell_area_multiple": max_cell_area_multiple,
                "min_cell_inside_pct": min_cell_inside_pct,
                "max_cell_outside_pct": max_cell_outside_pct,
                "simplify_tolerance_m": simplify_tolerance_m,
                "note": "V5 fits closed road/rail cells to the input polygon. It is not a pedestrian/driving route.",
            },
            "geometry": result["snapped_geojson"],
        }

        snapped_boundary_feature = {
            "type": "Feature",
            "properties": {
                "name": "snapped_boundary_v5",
                "target": target_label,
                "road_tier": road_tier_label,
                "fit_mode": fit_mode_label,
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
            file_name="snapped_polygon_v5.geojson",
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
