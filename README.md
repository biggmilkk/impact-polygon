# Closed Loop Road/Rail Snapper for Streamlit

This Streamlit app lets a user draw a rough polygon and snaps it to nearby OpenStreetMap road/rail network linework.

## What V3 fixes

V1 snapped points independently and connected them with straight lines. That created fake diagonal shortcuts.

V2 used actual OSM linework and avoided fake diagonals, but it could return broken pieces because it did not care whether the selected roads could form a loop.

V3 prioritizes a connected closed loop:

1. Sample the drawn polygon boundary into control points.
2. Find several nearby OSM road/rail graph nodes for every control point.
3. Use a cyclic dynamic program to choose nodes that are close to the boundary and can connect back to the start.
4. Route between those chosen nodes along the OSM network.
5. Return real road/rail geometry as GeoJSON.

This means the output can move inward or outward from the drawn polygon if a nearby road forms a better closed loop.

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

1. Push these files to GitHub.
2. Create a new Streamlit app.
3. Select your GitHub repo.
4. Set the main file path to `app.py`.
5. Deploy.

## Recommended starting settings

For dense city blocks:

- Snap to: Roads only
- Search buffer: 250-400m
- Loop control-point spacing: 50-80m
- Max candidate distance: 150-250m
- Nearby candidates per control point: 5-7
- Boundary closeness weight: 5-8
- Max control points: 60-80

## Tuning guide

If the result does not close:

- Increase search buffer.
- Increase max candidate distance.
- Increase nearby candidates per control point.
- Use Roads only instead of Roads + rail.

If the result closes but drifts too far from the drawn polygon:

- Increase boundary closeness weight.
- Lower loop control-point spacing.

If the result is slow:

- Increase loop control-point spacing.
- Lower max control points.
- Use Roads only.

## Important note

A closed road network loop may not exist near every drawn polygon. When the app cannot find a fully closed connected loop, it returns the best connected open sequence and displays a warning.
