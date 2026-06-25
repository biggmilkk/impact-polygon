# Road/Rail Polygon Snapper V5

A Streamlit MVP for snapping a user-drawn polygon to nearby OpenStreetMap road/rail linework.

V5 is for **polygon boundaries**, not walking/driving routes. It ignores one-way direction, pedestrian crossings, and routing rules. The app builds closed road-network cells and selects the best-fitting closed polygon against the user's drawn polygon.

## What V5 fixes

Compared with V4:

- Keeps the **best-fitting** closed component, not the largest component.
- Scores the whole output polygon against the input polygon using coverage, outside bulge, missing area, boundary distance, area difference, and simplicity.
- Removes/adds whole closed road cells to improve the fit.
- Avoids red shapes that expand into unrelated neighborhoods.
- Defaults to **Main roads only**, so small side roads are less likely to create noisy boundaries.
- Still excludes pedestrian paths, footways, crossings, cycleways, tracks, and steps.
- Excludes service roads unless explicitly enabled.

## Files

```text
app.py
snapper.py
requirements.txt
README.md
.gitignore
```

## Local setup

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

## Streamlit Cloud deployment

1. Push these files to GitHub.
2. Go to Streamlit Community Cloud.
3. Create a new app from the repo.
4. Use `app.py` as the main file.
5. Deploy.

## Recommended settings for clean polygons

Start with:

```text
Snap to: Roads only
Road tier: Main roads only
Fit behavior: Tight / avoid outside bulges
Search buffer: 300-400 m
Ignore tiny closed cells below: 750 m2
Ignore very large cells above input-area multiple: 2.0
Minimum cell overlap with input polygon: 35%
Maximum outside share for center-inside cells: 65%
Simplify output tolerance: 10-15 m
Prune dead-end branches: on
Best-fit refinement iterations: 30
```

If the red output is still too large:

```text
Fit behavior: Tight / avoid outside bulges
Lower max cell area multiple to 1.5
Raise minimum cell overlap to 45-55%
Lower max outside share to 45-55%
Use Arterial roads only
```

If the red output is too small or fails to close:

```text
Fit behavior: Cover input polygon more
Use Public streets, no service roads or paths
Increase search buffer to 500-700 m
Lower minimum cell overlap to 20-30%
Increase max cell area multiple to 3.0-4.0
```

## How it works

1. The user draws a polygon.
2. The app queries nearby OpenStreetMap roads/rails.
3. It filters the network to the selected road tier.
4. It makes the network undirected.
5. It prunes dead-end branches.
6. It polygonizes the linework into closed road cells.
7. It selects plausible cells that overlap the drawn polygon.
8. It scores the full output polygon, not just individual cells.
9. It greedily removes cells that create outside bulges and adds cells only when they improve fit.
10. It keeps the best-fitting closed component.
11. It simplifies the final output.
12. It returns GeoJSON.

## Limitations

- This uses road/rail centerlines, not curb, parcel, or administrative boundaries.
- If roads do not form closed cells around the drawn polygon, the app may return a smaller/larger nearby road-cell polygon.
- Overpasses and bridges may still create artificial cells because OSM centerlines can geometrically cross.
- Rail lines can split cells in unexpected ways; Roads only is usually cleaner.
- Live OSM/Overpass queries can be slow for large polygons. For production, use local OSM data with PostGIS/pgRouting or a prebuilt network service.
