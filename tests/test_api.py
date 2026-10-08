import io
import zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)
SAMPLES_DIR = Path(__file__).parents[1] / "samples"
KML_SAMPLE = SAMPLES_DIR / "example.kml"
SHP_SAMPLE = SAMPLES_DIR / "example-shapefile.zip"


def test_kml_upload_and_measurement():
    with KML_SAMPLE.open("rb") as file:
        response = client.post("/api/files/", files={"file": ("example.kml", file, "application/vnd.google-earth.kml+xml")})
    assert response.status_code == 201
    file_id = response.json()["id"]

    info_resp = client.get(f"/api/files/{file_id}/")
    assert info_resp.status_code == 200
    info = info_resp.json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 2
    assert info["crs"] == "EPSG:4326"

    meas_resp = client.get(f"/api/files/{file_id}/measurements/")
    assert meas_resp.status_code == 200
    measurements = meas_resp.json()["measurements"]
    assert len(measurements) == 2

    polygon_feat = measurements[0]
    assert polygon_feat["geometry_type"] == "Polygon"
    area_m2 = polygon_feat["measurement"]["area_m2"]
    assert 900_000 < area_m2 < 1_100_000, f"Expected ~1,000,000 sq meters, got {area_m2}"

    line_feat = measurements[1]
    assert line_feat["geometry_type"] == "LineString"
    assert line_feat["measurement"]["length_m"] > 0


def test_shapefile_upload_and_measurement():
    with SHP_SAMPLE.open("rb") as file:
        response = client.post("/api/files/", files={"file": ("example-shapefile.zip", file, "application/zip")})
    assert response.status_code == 201
    file_id = response.json()["id"]

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] >= 1

    measurements = client.get(f"/api/files/{file_id}/measurements/").json()["measurements"]
    assert len(measurements) >= 1


def test_rejects_wrong_extension():
    response = client.post("/api/files/", files={"file": ("bad.txt", b"hello", "text/plain")})
    assert response.status_code == 415


def test_rejects_empty_file():
    response = client.post("/api/files/", files={"file": ("empty.kml", b"", "application/vnd.google-earth.kml+xml")})
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_rejects_corrupt_zip():
    response = client.post("/api/files/", files={"file": ("corrupt.zip", b"not a zip content", "application/zip")})
    assert response.status_code == 422


def test_rejects_zip_without_shp():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "no shapefile here")
    buf.seek(0)
    response = client.post("/api/files/", files={"file": ("noshp.zip", buf, "application/zip")})
    assert response.status_code == 422
    assert "must contain a .shp file" in response.json()["detail"]


def test_rejects_invalid_kml():
    response = client.post("/api/files/", files={"file": ("invalid.kml", b"<xml>broken kml</xml>", "application/vnd.google-earth.kml+xml")})
    assert response.status_code == 422


def test_missing_file_is_404():
    response = client.get("/api/files/nonexistent-id-12345/")
    assert response.status_code == 404

