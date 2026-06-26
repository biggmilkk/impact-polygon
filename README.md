# Road Polygon Snapper V11

A Streamlit prototype for snapping a rough user-drawn polygon to a clean road/rail-bounded polygon using OpenStreetMap data.

## What V11 changes

V11 is tuned for a simpler end-user experience:

- Keeps the UI to two main sliders: **Fit** and **Boundary detail**.
- Defaults to a smoother outline using main roads first.
- Uses normal public streets only when the user moves Boundary detail far right or when the algorithm needs a fallback.
- Adds a hidden final outline cleanup step to remove small side-street teeth, narrow spikes, and triangular protrusions.
- Reduces default search size and internal retries for faster runs.
- Projects the OSM graph once per run before hidden attempts to reduce repeated work.
- Keeps debug ZIP export for reporting failures.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Push these files to GitHub.
2. Create a new Streamlit app.
3. Set the main file path to `app.py`.
4. Deploy.

## User controls

**Fit**

- Move left if the red polygon bulges too far outside the blue drawing.
- Move right if the red polygon misses too much of the blue drawing.

**Boundary detail**

- Move left for smoother/faster/fewer points.
- Move right if smaller normal streets are needed to form a closed polygon.

## Debugging

If an output is wrong, export the debug ZIP and send it with a screenshot showing:

- blue = input polygon
- red = algorithm output
- purple = desired output drawn manually

The debug ZIP contains the input/output GeoJSON, settings, metrics, and an HTML map.
