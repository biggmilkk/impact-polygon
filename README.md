# Road Polygon Snapper V9

A simple Streamlit prototype for drawing a rough polygon and snapping it to a clean road-bounded outline using OpenStreetMap data.

## What V9 changes

V9 fixes the "small output polygon" failure mode seen in dense city grids:

- The app now defaults to a balanced Fit and normal public streets, while still excluding footways, crossings, cycleways, tracks, steps, and service roads.
- The algorithm penalizes tiny polygons that cover only a small part of the input.
- It tries multiple road-cell seed selections instead of getting stuck on the first neat closed cell.
- It can automatically fall back from large/main roads to normal public streets when the larger-road network cannot form a good enclosing loop.
- Interior holes are removed so the user sees one outer polygon boundary.

## User controls

The main interface exposes only two sliders:

- **Fit**: left = tighter/smaller, right = expand/cover more of the drawn polygon.
- **Boundary detail**: left = smoother/fewer points/larger roads, right = sharper/normal streets.

Rail lines are optional and off by default.

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

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repo.
2. Create a new app in Streamlit Community Cloud.
3. Select the repo and branch.
4. Set the main file path to `app.py`.
5. Deploy.

## Debugging bad cases

After snapping, open **Export / debug files** and download the debug ZIP. Share that ZIP with a screenshot showing:

- blue = input polygon
- red = algorithm output
- purple = desired output drawn manually on the screenshot

The debug ZIP contains the input GeoJSON, output GeoJSON, settings, metrics, and a standalone HTML map.
