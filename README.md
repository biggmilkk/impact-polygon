# Road/Rail Polygon Snapper V4

A Streamlit MVP for snapping a user-drawn polygon to nearby OpenStreetMap road/rail linework.

V4 is designed for polygon geometry, not routing. It does **not** obey one-way streets, pedestrian crossings, or walking rules. It builds closed road-network cells and returns a polygon boundary snapped to roads/rails.

## What V4 fixes

Compared with earlier versions:

- Prioritizes a closed polygon, not a shortest human path.
- Prunes dead-end branches so red lines do not poke out into nowhere.
- Excludes pedestrian paths, crossings, tracks, cycleways, and footways by default.
- Excludes service roads by default.
- Lets you switch to main roads only if you want to ignore smaller side streets.
- Simplifies the output so the snapped polygon has fewer coordinate points.

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

## Recommended settings

For clean polygons:

```text
Snap to: Roads only
Road tier: Public streets, no service roads or paths
Search buffer: 300 m
Cell inclusion threshold: 20%
Ignore tiny cells below: 500 m²
Simplify output tolerance: 8 m
Keep largest component: on
Prune dead-end branches: on
```

If the polygon still includes too many side roads:

```text
Road tier: Main roads only
```

If the polygon becomes too coarse or cannot find cells:

```text
Road tier: Public streets, no service roads or paths
Increase search buffer
Lower cell inclusion threshold
Lower ignore tiny cells threshold
```

## How it works

1. The user draws a polygon.
2. The app queries nearby OpenStreetMap roads/rails.
3. It filters the network to the selected road tier.
4. It converts the network into undirected linework.
5. It prunes dead-end branches.
6. It polygonizes the remaining road/rail linework into closed cells.
7. It selects cells that overlap the drawn polygon.
8. It dissolves selected cells into one snapped polygon.
9. It simplifies the output to reduce coordinate count.
10. It returns GeoJSON.

## Limitations

- This uses road/rail centerlines, not curb or parcel boundaries.
- If roads do not form closed cells in the selected area, the app may fail or return a simpler nearby component.
- Rail lines may create unexpected cells when mixed with roads.
- Live OSM/Overpass queries can be slow for very large polygons. For production, use local OSM data with PostGIS/pgRouting or a tiled/network service.
