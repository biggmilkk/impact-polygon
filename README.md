# Road Polygon Snapper V10

A Streamlit prototype for drawing a rough polygon and snapping it to a clean closed polygon formed by nearby OpenStreetMap roads, with optional rail-line support.

## What V10 changes

V10 focuses on the latest failure cases where the red output became too small even though a larger, obvious road-bounded outline existed.

Changes:

- Adds an **outline capture** pass so cells just outside the blue input polygon can be considered.
- Raises the internal coverage target so a small internal road block is much less likely to win.
- Penalizes outputs that ignore large parts of the blue outline or its vertices.
- Keeps the normal user interface simple: only **Fit**, **Boundary detail**, and an optional rail checkbox.
- Adds a cache-buster to the Streamlit cached snap function, so old red outputs should not survive app updates.
- Adds a hidden **Advanced > Clear cached snap results** button for debugging.

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

## User workflow

1. Draw one rough polygon.
2. Click **Snap polygon**.
3. Adjust **Fit** if the red output is too small or too large.
4. Adjust **Boundary detail** if the red output is too jagged or needs smaller streets.
5. Use **Export / debug files** only when you need to share a failed case.

## Debug workflow

For algorithm refinement, export the debug ZIP and share it with a screenshot where:

- blue = input polygon
- red = algorithm output
- purple = desired output, drawn manually on the screenshot

The debug ZIP contains input/output GeoJSON, settings, metrics, and an HTML map.
