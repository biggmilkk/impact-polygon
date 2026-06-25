# Polygon Road/Rail Snapper for Streamlit

This is a Streamlit MVP that lets a user draw a polygon and snap the polygon boundary to nearby OpenStreetMap roads and rail lines.

## V2 behavior

The first version connected independently snapped points with straight red lines. That creates ugly diagonal shortcuts across streets.

This version fixes that by outputting actual OpenStreetMap road/rail linework:

1. Sample the polygon boundary.
2. Find nearby road/rail candidates for each sample.
3. Use a road-switch penalty to reduce zig-zags between parallel roads.
4. Extract the actual OSM line segments between consecutive samples on the same road/rail feature.
5. Do not connect transitions that would create fake diagonal lines.

The output may be a `MultiLineString` instead of one perfectly closed polygon. That is expected when nearby roads do not form a continuous clean boundary.

## Files

```text
app.py
snapper.py
requirements.txt
README.md
.gitignore
```

## Local run

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

1. Push this folder to GitHub.
2. Create a new Streamlit app.
3. Select your GitHub repo.
4. Set the main file path to `app.py`.
5. Deploy.

## Recommended settings for dense cities

- Search buffer: 150-250m
- Boundary sample spacing: 10-20m
- Max snap distance: 80-150m
- Nearby candidates: 5
- Road-switch penalty: 30-60m

Increase the road-switch penalty if the snapped line jumps between parallel roads. Lower it if it sticks to the wrong road for too long.
