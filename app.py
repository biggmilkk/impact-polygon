from __future__ import annotations

import hashlib
import html
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

import folium
import streamlit as st
from folium.plugins import Draw
from shapely.geometry import shape
from shapely.ops import unary_union
from streamlit_folium import st_folium

from snapper import snap_polygon_to_road_rail_polygon


APP_VERSION = "v7_simple_ux"


st.set_page_config(
    page_title="Road Polygon Snapper",
    page_icon="🗺️",
    layout="wide",
)

st.title("🗺️ Road Polygon Snapper")
st.caption("Draw a rough polygon, snap it to nearby roads, then adjust with two simple controls if needed.")


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
    max_refinement_iterations: int,
) -> dict[str, Any]:
    drawn_geojson = json.loads(drawn_geojson_string)
    result = snap_polygon_to_road_rail_polygon(
        drawn_geojson=drawn_geojson,
        target=target,  # type: ignore[arg-type]
        road_tier=road_tier,  # type: ignore[arg-type]
        fit_mode=fit_mode,  # type: ignore[arg-type]
        search_buffer_m=float(search_buffer_m),
        min_cell_area_m2=float(min_cell_area_m2),
        max_cell_area_multiple=float(max_cell_area_multiple),
        min_cell_inside_ratio=float(min_cell_inside_ratio),
        max_cell_outside_ratio=float(max_cell_outside_ratio),
        simplify_tolerance_m=float(simplify_tolerance_m),
        prune_dead_ends=True,
        max_refinement_iterations=int(max_refinement_iterations),
    )
    return result.__dict__


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _json_download(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def _round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def _derive_simple_settings(fit_slider: int, detail_slider: int, include_rail: bool) -> dict[str, Any]:
    """Translate two human-facing sliders into the algorithm's internal parameters."""
    fit = max(0.0, min(1.0, float(fit_slider) / 100.0))
    detail = max(0.0, min(1.0, float(detail_slider) / 100.0))

    if fit_slider <= 40:
        fit_mode = "tight"
        fit_label = "tight / avoids outside bulges"
    elif fit_slider <= 65:
        fit_mode = "balanced"
        fit_label = "balanced inward/outward"
    else:
        fit_mode = "cover"
        fit_label = "expanded / covers more of the drawing"

    if detail_slider < 25:
        road_tier = "arterial"
        road_label = "largest roads only"
    elif detail_slider < 75:
        road_tier = "main"
        road_label = "main roads"
    else:
        road_tier = "public"
        road_label = "main roads plus smaller public streets"

    target = "roads_and_rails" if include_rail else "roads"
    target_label = "roads + rail lines" if include_rail else "roads only"

    # Fit slider: left = tighter/contracted, right = looser/expanded.
    search_buffer_m = _round_to(225 + (425 * fit) + (150 * detail), 25)
    max_cell_area_multiple = round(1.35 + (3.15 * fit), 2)
    min_cell_inside_ratio = round(0.62 - (0.43 * fit), 2)
    max_cell_outside_ratio = round(0.28 + (0.55 * fit), 2)

    # Detail slider: left = smoother/fewer points, right = sharper/more small roads.
    min_cell_area_m2 = _round_to(3000 - (2700 * detail), 50)
    simplify_tolerance_m = round(28 - (23 * detail), 1)
    max_refinement_iterations = int(round(12 + (28 * detail) + (10 * fit)))

    return {
        "app_version": APP_VERSION,
        "fit_slider": int(fit_slider),
        "detail_slider": int(detail_slider),
        "include_rail": bool(include_rail),
        "target": target,
        "target_label": target_label,
        "road_tier": road_tier,
        "road_label": road_label,
        "fit_mode": fit_mode,
        "fit_label": fit_label,
        "search_buffer_m": float(search_buffer_m),
        "min_cell_area_m2": float(min_cell_area_m2),
        "max_cell_area_multiple": float(max_cell_area_multiple),
        "min_cell_inside_ratio": float(min_cell_inside_ratio),
        "max_cell_outside_ratio": float(max_cell_outside_ratio),
        "simplify_tolerance_m": float(simplify_tolerance_m),
        "prune_dead_ends": True,
        "max_refinement_iterations": int(max_refinement_iterations),
    }


def _as_feature(name: str, role: str, geometry: dict[str, Any], properties: dict[str, Any] | None = None) -> dict[str, Any]:
    props = {"name": name, "role": role}
    if properties:
        props.update(properties)
    return {"type": "Feature", "properties": props, "geometry": geometry}


def _input_feature_from_drawn(drawn: dict[str, Any]) -> dict[str, Any]:
    return _as_feature(
        name="input_polygon_blue",
        role="input_polygon",
        geometry=drawn["geometry"],
        properties={"display_color": "blue", "note": "Original polygon drawn by the user."},
    )


def _metrics_from_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "algorithm",
        "network_nodes_count",
        "network_edges_count",
        "candidate_cells_count",
        "initially_selected_cells_count",
        "final_selected_cells_count",
        "output_area_m2",
        "output_perimeter_m",
        "coordinate_count",
        "closed_loop",
        "fit_score",
        "coverage_ratio",
        "outside_ratio",
        "missing_ratio",
        "warning",
    ]
    return {key: result.get(key) for key in keys}


def _build_export_objects(
    *,
    drawn: dict[str, Any],
    result: dict[str, Any],
    settings_used: dict[str, Any],
    issue_notes: str,
    case_id: str | None = None,
) -> dict[str, Any]:
    created_at = _utc_stamp()
    if case_id is None:
        case_id = f"road_snapper_{created_at}"

    input_feature = _input_feature_from_drawn(drawn)
    output_polygon_feature = _as_feature(
        name="algorithm_output_polygon_red",
        role="algorithm_output_polygon",
        geometry=result["snapped_geojson"],
        properties={
            "display_color": "red",
            "algorithm": result.get("algorithm"),
            "note": "Polygon returned by the snapping algorithm.",
        },
    )
    output_boundary_feature = _as_feature(
        name="algorithm_output_boundary_red",
        role="algorithm_output_boundary",
        geometry=result["snapped_boundary_geojson"],
        properties={
            "display_color": "red",
            "algorithm": result.get("algorithm"),
            "note": "Boundary of the returned snapped polygon.",
        },
    )
    query_area_feature = _as_feature(
        name="osm_query_area_gray",
        role="osm_query_area",
        geometry=result["query_area_geojson"],
        properties={"display_color": "gray", "note": "Buffered area used to query nearby roads/rails."},
    )

    combined_geojson = {
        "type": "FeatureCollection",
        "features": [input_feature, output_polygon_feature, output_boundary_feature],
    }

    debug_bundle = {
        "case_id": case_id,
        "created_at_utc": created_at,
        "app_version": APP_VERSION,
        "settings_used": settings_used,
        "metrics": _metrics_from_result(result),
        "input_polygon_feature": input_feature,
        "algorithm_output_polygon_feature": output_polygon_feature,
        "algorithm_output_boundary_feature": output_boundary_feature,
        "osm_query_area_feature": query_area_feature,
        "combined_input_output_geojson": combined_geojson,
        "user_notes": issue_notes,
    }

    return {
        "case_id": case_id,
        "input_feature": input_feature,
        "output_polygon_feature": output_polygon_feature,
        "output_boundary_feature": output_boundary_feature,
        "query_area_feature": query_area_feature,
        "combined_geojson": combined_geojson,
        "debug_bundle": debug_bundle,
    }


def _bounds_for_features(features: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    geometries = []
    for feature in features:
        geom_dict = feature.get("geometry")
        if geom_dict:
            try:
                geom = shape(geom_dict)
            except Exception:  # noqa: BLE001
                continue
            if not geom.is_empty:
                geometries.append(geom)

    if not geometries:
        return (103.8198, 1.3521, 103.8198, 1.3521)

    return unary_union(geometries).bounds


def _build_debug_map_html(export_objects: dict[str, Any], settings_used: dict[str, Any], metrics: dict[str, Any]) -> str:
    features_for_bounds = [
        export_objects["input_feature"],
        export_objects["output_polygon_feature"],
        export_objects["query_area_feature"],
    ]
    minx, miny, maxx, maxy = _bounds_for_features(features_for_bounds)
    center = [(miny + maxy) / 2.0, (minx + maxx) / 2.0]

    debug_map = folium.Map(location=center, zoom_start=16, tiles="OpenStreetMap", control_scale=True)

    folium.GeoJson(
        export_objects["query_area_feature"],
        name="Search area",
        style_function=lambda _: {"color": "gray", "weight": 1, "fillOpacity": 0.02, "dashArray": "6,6"},
    ).add_to(debug_map)

    folium.GeoJson(
        export_objects["input_feature"],
        name="Input polygon - blue",
        style_function=lambda _: {"color": "blue", "weight": 3, "fillOpacity": 0.04},
    ).add_to(debug_map)

    folium.GeoJson(
        export_objects["output_polygon_feature"],
        name="Output polygon - red",
        style_function=lambda _: {"color": "red", "weight": 4, "fillColor": "red", "fillOpacity": 0.08},
    ).add_to(debug_map)

    folium.GeoJson(
        export_objects["output_boundary_feature"],
        name="Output boundary - red",
        style_function=lambda _: {"color": "red", "weight": 5},
    ).add_to(debug_map)

    settings_text = html.escape(json.dumps(settings_used, indent=2))
    metrics_text = html.escape(json.dumps(metrics, indent=2))
    legend_html = f"""
    <div style="
        position: fixed;
        bottom: 24px;
        left: 24px;
        z-index: 9999;
        width: 360px;
        max-height: 360px;
        overflow: auto;
        background: white;
        border: 2px solid #666;
        border-radius: 6px;
        padding: 10px;
        font-size: 12px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.25);
    ">
      <b>Road Snapper debug map</b><br>
      <span style="color: blue; font-weight: bold;">Blue</span>: input polygon<br>
      <span style="color: red; font-weight: bold;">Red</span>: algorithm output<br>
      <span style="color: gray; font-weight: bold;">Gray</span>: search area<br>
      <details><summary>Simple settings used</summary><pre>{settings_text}</pre></details>
      <details><summary>Metrics</summary><pre>{metrics_text}</pre></details>
    </div>
    """
    debug_map.get_root().html.add_child(folium.Element(legend_html))
    folium.LayerControl().add_to(debug_map)
    debug_map.fit_bounds([[miny, minx], [maxy, maxx]])
    return debug_map.get_root().render()


def _build_debug_zip(
    *,
    export_objects: dict[str, Any],
    settings_used: dict[str, Any],
    metrics: dict[str, Any],
    debug_map_html: str,
) -> bytes:
    input_fc = {"type": "FeatureCollection", "features": [export_objects["input_feature"]]}
    output_fc = {
        "type": "FeatureCollection",
        "features": [export_objects["output_polygon_feature"], export_objects["output_boundary_feature"]],
    }
    readme = """Road Polygon Snapper debug bundle

Send this ZIP with your screenshot when reporting a bad output.

Files:
- debug_bundle.json: complete case data
- input_polygon.geojson: blue polygon drawn in the app
- snapped_output.geojson: red output polygon and boundary
- input_output_combined.geojson: blue + red together
- settings.json: simple sliders plus internal parameters
- metrics.json: algorithm fit metrics
- debug_map.html: interactive map with blue input and red output

For now, your desired purple output can stay hand-drawn on the screenshot.
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README_DEBUG.txt", readme)
        zf.writestr("debug_bundle.json", _json_download(export_objects["debug_bundle"]))
        zf.writestr("input_polygon.geojson", _json_download(input_fc))
        zf.writestr("snapped_output.geojson", _json_download(output_fc))
        zf.writestr("input_output_combined.geojson", _json_download(export_objects["combined_geojson"]))
        zf.writestr("settings.json", _json_download(settings_used))
        zf.writestr("metrics.json", _json_download(metrics))
        zf.writestr("debug_map.html", debug_map_html)
    return buffer.getvalue()


def _clear_previous_result_if_needed(drawn_geojson_string: str, current_case_key: str) -> None:
    previous_drawn_string = st.session_state.get("drawn_geojson_string_v7")
    previous_case_key = st.session_state.get("snap_result_case_key_v7")
    if previous_drawn_string and previous_drawn_string != drawn_geojson_string:
        st.session_state.pop("snap_result_v7", None)
        st.session_state.pop("snap_settings_v7", None)
        st.session_state.pop("drawn_geojson_v7", None)
        st.session_state.pop("snap_result_case_key_v7", None)
        st.session_state.pop("snap_result_case_id_v7", None)
    elif previous_case_key and previous_case_key != current_case_key:
        st.session_state.pop("snap_result_v7", None)
    st.session_state["drawn_geojson_string_v7"] = drawn_geojson_string


st.markdown(
    """
    **How to use:** draw one rough polygon, click **Snap polygon**, then adjust only these two controls if the result is not right.
    """
)

left, right = st.columns([0.64, 0.36], gap="large")

with left:
    st.subheader("1. Draw")

    m = folium.Map(location=[1.3521, 103.8198], zoom_start=12, tiles="OpenStreetMap", control_scale=True)

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
        key="draw_map_v7",
    )

with right:
    st.subheader("2. Adjust")

    fit_slider = st.slider(
        "Fit",
        min_value=0,
        max_value=100,
        value=35,
        step=5,
        help="Move left when the red polygon bulges too far outside the blue drawing. Move right when the red polygon misses too much of the blue drawing.",
    )
    st.caption("← tighter / smaller ··········································· expand / cover more →")

    detail_slider = st.slider(
        "Boundary detail",
        min_value=0,
        max_value=100,
        value=40,
        step=5,
        help="Move left for a cleaner shape with fewer points and bigger roads. Move right if the boundary needs smaller public streets to close properly.",
    )
    st.caption("← smoother / fewer points ····························· sharper / smaller roads →")

    include_rail = st.checkbox(
        "Allow rail lines as boundaries",
        value=False,
        help="Leave this off unless railway lines should count as valid polygon edges.",
    )

    settings_now = _derive_simple_settings(
        fit_slider=fit_slider,
        detail_slider=detail_slider,
        include_rail=include_rail,
    )

    st.info(
        f"Current behavior: **{settings_now['fit_label']}**, **{settings_now['road_label']}**, "
        f"**{settings_now['target_label']}**."
    )

    drawings = map_data.get("all_drawings") if map_data else None

    if not drawings:
        st.write("Draw one polygon on the map to begin.")
        st.stop()

    drawn = drawings[-1]
    drawn_geojson_string = json.dumps(drawn, sort_keys=True)
    current_case_key = json.dumps({"drawn": drawn, "settings": settings_now}, sort_keys=True)
    current_case_id = "road_snapper_" + hashlib.sha1(current_case_key.encode("utf-8")).hexdigest()[:12]

    _clear_previous_result_if_needed(drawn_geojson_string, current_case_key)

    if st.button("Snap polygon", type="primary", use_container_width=True):
        with st.spinner("Finding a clean closed road polygon..."):
            try:
                result = snap_cached(
                    drawn_geojson_string=drawn_geojson_string,
                    target=settings_now["target"],
                    road_tier=settings_now["road_tier"],
                    fit_mode=settings_now["fit_mode"],
                    search_buffer_m=float(settings_now["search_buffer_m"]),
                    min_cell_area_m2=float(settings_now["min_cell_area_m2"]),
                    max_cell_area_multiple=float(settings_now["max_cell_area_multiple"]),
                    min_cell_inside_ratio=float(settings_now["min_cell_inside_ratio"]),
                    max_cell_outside_ratio=float(settings_now["max_cell_outside_ratio"]),
                    simplify_tolerance_m=float(settings_now["simplify_tolerance_m"]),
                    max_refinement_iterations=int(settings_now["max_refinement_iterations"]),
                )
                st.session_state["snap_result_v7"] = result
                st.session_state["snap_settings_v7"] = settings_now
                st.session_state["drawn_geojson_v7"] = drawn
                st.session_state["snap_result_case_key_v7"] = current_case_key
                st.session_state["snap_result_case_id_v7"] = current_case_id
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
                st.stop()

    result = st.session_state.get("snap_result_v7")
    result_case_key = st.session_state.get("snap_result_case_key_v7")

    if result and result_case_key != current_case_key:
        st.info("The polygon or controls changed. Click **Snap polygon** again to update the red output.")
        result = None

    if result:
        settings_used = st.session_state.get("snap_settings_v7", settings_now)
        drawn_for_result = st.session_state.get("drawn_geojson_v7", drawn)

        if result.get("warning"):
            st.warning(result["warning"])

        st.subheader("3. Result")
        status = "Closed polygon found" if result.get("closed_loop") else "No clean closed polygon found"
        st.success(status) if result.get("closed_loop") else st.warning(status)

        c1, c2, c3 = st.columns(3)
        c1.metric("Input covered", f"{result['coverage_ratio'] * 100:.0f}%")
        c2.metric("Outside bulge", f"{result['outside_ratio'] * 100:.0f}%")
        c3.metric("Points", result["coordinate_count"])

        tips: list[str] = []
        if result["outside_ratio"] > 0.45:
            tips.append("Result is expanding too far: move **Fit** left.")
        if result["coverage_ratio"] < 0.60:
            tips.append("Result is missing too much: move **Fit** right, or move **Boundary detail** right.")
        if result["coordinate_count"] > 250:
            tips.append("Result is too jagged: move **Boundary detail** left.")
        if tips:
            st.caption(" ".join(tips))
        else:
            st.caption("Looks reasonable. Use the export section only when you want to save or share the case.")

        issue_notes = st.text_area(
            "Optional issue note for export",
            placeholder="Example: red output bulges too far west; preferred purple line follows the main road.",
            height=80,
        )

        export_objects = _build_export_objects(
            drawn=drawn_for_result,
            result=result,
            settings_used=settings_used,
            issue_notes=issue_notes,
            case_id=st.session_state.get("snap_result_case_id_v7", current_case_id),
        )
        metrics = _metrics_from_result(result)
        debug_map_html = _build_debug_map_html(export_objects, settings_used, metrics)
        debug_zip = _build_debug_zip(
            export_objects=export_objects,
            settings_used=settings_used,
            metrics=metrics,
            debug_map_html=debug_map_html,
        )

        with st.expander("Export / debug files", expanded=False):
            st.download_button(
                "Download debug ZIP",
                data=debug_zip,
                file_name=f"{export_objects['case_id']}_debug.zip",
                mime="application/zip",
                use_container_width=True,
            )

            input_fc = {"type": "FeatureCollection", "features": [export_objects["input_feature"]]}
            st.download_button(
                "Download input polygon GeoJSON",
                data=_json_download(input_fc),
                file_name=f"{export_objects['case_id']}_input.geojson",
                mime="application/geo+json",
                use_container_width=True,
            )
            st.download_button(
                "Download input + output GeoJSON",
                data=_json_download(export_objects["combined_geojson"]),
                file_name=f"{export_objects['case_id']}_input_output.geojson",
                mime="application/geo+json",
                use_container_width=True,
            )
            st.download_button(
                "Download HTML map",
                data=debug_map_html,
                file_name=f"{export_objects['case_id']}_map.html",
                mime="text/html",
                use_container_width=True,
            )

        with st.expander("Technical settings used", expanded=False):
            st.json(settings_used)
            st.json(metrics)

    else:
        st.write("Click **Snap polygon** after drawing your polygon.")


result = st.session_state.get("snap_result_v7")
result_case_key = st.session_state.get("snap_result_case_key_v7")

if result and result_case_key == json.dumps(
    {
        "drawn": st.session_state.get("drawn_geojson_v7"),
        "settings": st.session_state.get("snap_settings_v7"),
    },
    sort_keys=True,
):
    # This equality check only passes when the preview is still tied to the saved result.
    pass

# Render preview based on the saved result, even if the user is about to change settings.
result = st.session_state.get("snap_result_v7")
if result:
    drawn_for_result = st.session_state.get("drawn_geojson_v7")
    settings_used = st.session_state.get("snap_settings_v7")
    if drawn_for_result and settings_used:
        st.divider()
        st.subheader("Preview")

        export_objects = _build_export_objects(
            drawn=drawn_for_result,
            result=result,
            settings_used=settings_used,
            issue_notes="",
            case_id=st.session_state.get("snap_result_case_id_v7"),
        )

        minx, miny, maxx, maxy = _bounds_for_features(
            [export_objects["input_feature"], export_objects["output_polygon_feature"]]
        )
        preview_center = [(miny + maxy) / 2, (minx + maxx) / 2]
        preview = folium.Map(location=preview_center, zoom_start=16, tiles="OpenStreetMap", control_scale=True)

        folium.GeoJson(
            export_objects["input_feature"],
            name="Input polygon - blue",
            style_function=lambda _: {"color": "blue", "weight": 2, "fillOpacity": 0.04},
        ).add_to(preview)
        folium.GeoJson(
            export_objects["output_polygon_feature"],
            name="Snapped polygon - red",
            style_function=lambda _: {"color": "red", "weight": 4, "fillColor": "red", "fillOpacity": 0.08},
        ).add_to(preview)
        folium.GeoJson(
            export_objects["output_boundary_feature"],
            name="Snapped boundary - red",
            style_function=lambda _: {"color": "red", "weight": 5},
        ).add_to(preview)
        folium.LayerControl().add_to(preview)
        preview.fit_bounds([[miny, minx], [maxy, maxx]])
        st_folium(preview, height=520, width=None, key="preview_map_v7")
