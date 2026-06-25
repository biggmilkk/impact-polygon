# Polygon Road/Rail Snapper

A Streamlit MVP that lets a user draw a polygon and snaps the polygon boundary inward/outward to the nearest OpenStreetMap roads and rail lines.

## What it does

1. User draws a polygon in a Folium map.
2. The app samples points along the polygon boundary.
3. It queries OpenStreetMap for nearby roads and rail lines using OSMnx.
4. Each sampled boundary point is projected onto the nearest road/rail geometry.
5. The app returns a snapped GeoJSON LineString.

## Repo structure

```text
.
├── app.py
├── snapper.py
├── requirements.txt
├── .gitignore
└── README.md
```

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows PowerShell
pip install -r requirements.txt
streamlit run app.py
```

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. Go to Streamlit Community Cloud.
3. Create a new app.
4. Select your repo.
5. Set the main file path to `app.py`.
6. Deploy.

## Notes

- This app queries OpenStreetMap live, so very large polygons can be slow.
- For production, import a local OSM extract into PostGIS/pgRouting or use a dedicated tile/routing backend.
- The output is a snapped `LineString`, not a guaranteed valid polygon. Roads and rails do not always form a closed polygon.
