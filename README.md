# Geospatial File Measurement API

FastAPI web service for uploading KML files and Shapefile ZIP archives to compute metric polygon area and line length measurements via UTM reprojection.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

## API

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/files/` | Upload `.kml` or zipped Shapefile (`.zip`) |
| `GET` | `/api/files/{id}/` | Get file processing status and metadata |
| `GET` | `/api/files/{id}/measurements/` | Retrieve calculated geometric measurements |

### 1. Upload File
`curl -X POST -F "file=@samples/example.kml" http://127.0.0.1:8000/api/files/`
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-1234567890ab",
  "status": "COMPLETED"
}
```

### 2. Get File Info
`curl http://127.0.0.1:8000/api/files/a1b2c3d4-e5f6-7890-abcd-1234567890ab/`
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-1234567890ab",
  "filename": "example.kml",
  "feature_count": 2,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null
}
```

### 3. Get Measurements
`curl http://127.0.0.1:8000/api/files/a1b2c3d4-e5f6-7890-abcd-1234567890ab/measurements/`
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-1234567890ab",
  "status": "COMPLETED",
  "measurements": [
    {
      "feature_id": "0",
      "geometry_type": "Polygon",
      "crs": "EPSG:4326",
      "measurement_crs": "EPSG:32631",
      "measurement": {
        "area_m2": 1000000.0,
        "area_ha": 100.0,
        "length_m": null,
        "length_km": null,
        "note": null
      }
    }
  ]
}
```

## Architecture

- **Project Structure**: Clean single-file core (`app.py`), pytest test suite (`tests/`), SQLite metadata store, and sample dataset fixtures (`samples/`).
- **File Processing Flow**: Validates file extensions & size limit (50 MB) -> performs Zip-Slip safe extraction for shapefiles or XML parsing for KML -> converts to GeoPandas GeoDataFrame.
- **Measurement Flow**: Extracts geometry centroids -> reproject geometries from source CRS to metric projected CRS -> calculates accurate surface area/length.
- **CRS Handling**: Automatically calculates appropriate UTM zone (EPSG:326xx/327xx) based on longitude and hemisphere, preventing distortions from raw lat/lon degree measurements.

## Design Decisions

- **UTM Dynamic Zone Projection vs Raw Lat/Lon**: Used dynamic UTM projection per geometry centroid instead of raw degree calculations or fixed global projection to ensure accurate local metric units (sq. meters, meters).
- **Synchronous Upload Processing vs Asynchronous Worker Queue**: Handled processing synchronously in-request for simple assignment deployment; Celery + Redis worker pattern recommended for production loads.
- **SQLite + SQLAlchemy ORM vs PostGIS**: Used SQLite with JSON columns for zero-dependency standalone execution; easily swappable with PostgreSQL/PostGIS in production.
- **Strict Zip Validation vs Loose Extraction**: Enforced presence of `.shp`, `.shx`, and `.dbf` plus path-traversal (Zip-Slip) security validation during extract to prevent arbitrary file overwrites.

## Learnings

- Handling geospatial projections on lat/lon degrees directly yields non-metric degree-squared results; reprojection to local UTM projected CRS (EPSG:326xx) is essential for accurate SI metric calculations (`m²`, `ha`, `m`, `km`).
- KML files lack formal projection metadata headers and default to WGS84 (`EPSG:4326`), whereas Shapefiles require reading associated `.prj` files.

## Future Scope

- Asynchronous file processing queue using Celery/Redis for large multi-gigabyte spatial archives.
- PostGIS database backend integration for spatial indexing and bounding-box queries.
- GeoJSON export endpoint (`GET /api/files/{id}/export?format=geojson`).
- Authentication & rate-limiting middleware for API security.
