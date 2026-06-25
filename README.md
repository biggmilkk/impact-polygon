# Road Polygon Snapper V7

A simple Streamlit app for drawing a rough polygon and snapping it to nearby road/rail boundaries from OpenStreetMap.

V7 focuses on a simple user experience:

- Only two main sliders:
  - **Fit**: tighter/smaller vs expanded/cover more
  - **Boundary detail**: smoother/fewer points vs sharper/smaller roads
- Roads only by default
- Pedestrian paths, footways, crossings, tracks, cycleways, and steps are excluded by the snapping engine
- Service roads are not used unless the detail slider is pushed high enough to use smaller public streets; even then, service roads remain excluded
- Debug export is hidden in an expander

## Files

```text
app.py
snapper.py
requirements.txt
README.md
.gitignore
```

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Cloud

Deploy with:

- Main file path: `app.py`
- Python dependencies: `requirements.txt`

## How to tune outputs

Use only the two sliders first.

- If the red polygon bulges too far outside the blue drawing, move **Fit** left.
- If the red polygon misses too much of the blue drawing, move **Fit** right.
- If the red polygon is too jagged or has too many points, move **Boundary detail** left.
- If the red polygon cannot close or needs smaller streets, move **Boundary detail** right.
- Turn on **Allow rail lines as boundaries** only when railway lines should count as valid polygon edges.

## Debug bundle

After snapping, open **Export / debug files** and download the debug ZIP. It includes:

```text
debug_bundle.json
input_polygon.geojson
snapped_output.geojson
input_output_combined.geojson
settings.json
metrics.json
debug_map.html
README_DEBUG.txt
```

Share the ZIP with a screenshot if the output is bad.
