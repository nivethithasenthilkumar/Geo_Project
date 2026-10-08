from __future__ import annotations

import json
import logging
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import geopandas as gpd
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pyproj import CRS, Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform
from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "geospatial.db"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("geospatial-api")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class FileRecord(Base):
    __tablename__ = "files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    crs: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    measurements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(engine)
app = FastAPI(title="Geospatial File Measurement API", version="1.0.0")


def safe_zip_extract(path: Path, destination: Path) -> Path:
    dest_resolved = destination.resolve()
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if not any(Path(m.filename).suffix.lower() == ".shp" for m in members):
            raise ValueError("ZIP must contain a .shp file")
        required = {".shp", ".shx", ".dbf"}
        found = {Path(m.filename).suffix.lower() for m in members}
        if not required.issubset(found):
            raise ValueError("Shapefile ZIP must contain .shp, .shx, and .dbf files")
        for member in members:
            target = (destination / member.filename).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                raise ValueError("ZIP contains an unsafe path")
            if member.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    shp_files = list(destination.rglob("*.shp"))
    if not shp_files:
        raise ValueError("No .shp file found after extraction")
    return shp_files[0]


def parse_kml(path: Path) -> gpd.GeoDataFrame:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Invalid KML XML structure: {exc}")
    geometries: list[dict[str, Any]] = []
    properties: list[dict[str, Any]] = []
    for placemark in root.iter():
        if placemark.tag.rsplit("}", 1)[-1] != "Placemark":
            continue
        geometry_node = next((node for node in placemark.iter() if node.tag.rsplit("}", 1)[-1] in {"Point", "LineString", "Polygon", "MultiGeometry"}), None)
        if geometry_node is None:
            continue
        geometry = _kml_geometry(geometry_node)
        if geometry is not None:
            geometries.append(mapping(geometry))
            properties.append({node.tag.rsplit("}", 1)[-1]: (node.text or "") for node in placemark.iter() if node.tag.rsplit("}", 1)[-1] == "name"})
    if not geometries:
        raise ValueError("KML contains no supported geometries")
    return gpd.GeoDataFrame(properties, geometry=[shape(item) for item in geometries], crs="EPSG:4326")


def _kml_geometry(node: ET.Element):
    kind = node.tag.rsplit("}", 1)[-1]
    if kind == "MultiGeometry":
        parts = [_kml_geometry(child) for child in node if child.tag.rsplit("}", 1)[-1] in {"Point", "LineString", "Polygon", "MultiGeometry"}]
        from shapely.geometry import GeometryCollection
        valid_parts = [part for part in parts if part is not None]
        return GeometryCollection(valid_parts) if valid_parts else None
    coordinates = next((child.text for child in node.iter() if child.tag.rsplit("}", 1)[-1] == "coordinates"), None)
    if not coordinates or not coordinates.strip():
        return None
    raw_tokens = coordinates.strip().split()
    tuples = []
    for token in raw_tokens:
        parts = token.strip().split(",")
        if len(parts) >= 2:
            try:
                tuples.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    if not tuples:
        return None
    from shapely.geometry import Point, LineString, Polygon
    if kind == "Point": return Point(tuples[0])
    if kind == "LineString": return LineString(tuples)
    if kind == "Polygon": return Polygon(tuples)
    return None


def measurement_for(geometry, source_crs: CRS | str) -> tuple[dict[str, Any], str]:
    source = CRS.from_user_input(source_crs)
    if source.is_geographic:
        lon, lat = geometry.centroid.x, geometry.centroid.y
    else:
        wgs84 = CRS.from_epsg(4326)
        trans_to_wgs84 = Transformer.from_crs(source, wgs84, always_xy=True).transform
        wgs_centroid = transform(trans_to_wgs84, geometry.centroid)
        lon, lat = wgs_centroid.x, wgs_centroid.y

    zone = max(1, min(60, int((lon + 180) // 6) + 1))
    target = CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)
    projected = transform(Transformer.from_crs(source, target, always_xy=True).transform, geometry)

    result: dict[str, Any] = {"area_m2": None, "area_ha": None, "length_m": None, "length_km": None, "note": None}
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        result["area_m2"] = round(projected.area, 3)
        result["area_ha"] = round(projected.area / 10000, 6)
    elif geometry.geom_type in {"LineString", "MultiLineString"}:
        result["length_m"] = round(projected.length, 3)
        result["length_km"] = round(projected.length / 1000, 6)
    elif geometry.geom_type in {"Point", "MultiPoint"}:
        result["note"] = "point geometry has no area or length measurement"
    else:
        result["note"] = f"measurement not supported for this geometry type: {geometry.geom_type}"
    return result, target.to_string()


def process_file(record: FileRecord) -> None:
    record.status = "PROCESSING"
    with SessionLocal() as db:
        db.merge(record)
        db.commit()
    try:
        source = Path(record.stored_path)
        if source.suffix.lower() == ".zip":
            work = source.parent / f"extracted_{record.id}"
            work.mkdir(exist_ok=True)
            shp = safe_zip_extract(source, work)
            frame = gpd.read_file(shp)
            if frame.crs is None:
                raise ValueError("Shapefile has no CRS; include a .prj file")
        else:
            frame = parse_kml(source)
        source_crs = frame.crs.to_string() if frame.crs else "EPSG:4326"
        results = []
        for index, row in frame.iterrows():
            geometry = row.geometry
            item = {
                "feature_id": str(index),
                "geometry_type": geometry.geom_type,
                "geometry": mapping(geometry),
                "crs": source_crs,
                "properties": {key: str(value) for key, value in row.items() if key != "geometry"}
            }
            try:
                item["measurement"], item["measurement_crs"] = measurement_for(geometry, frame.crs or "EPSG:4326")
            except Exception as exc:
                item["measurement"] = {"note": f"measurement failed: {exc}"}
            results.append(item)
        with SessionLocal() as db:
            saved = db.get(FileRecord, record.id)
            if saved:
                saved.status, saved.feature_count, saved.crs, saved.measurements = "COMPLETED", len(results), source_crs, results
                db.commit()
    except Exception as exc:
        logger.exception("Failed to process %s", record.id)
        with SessionLocal() as db:
            saved = db.get(FileRecord, record.id)
            if saved:
                saved.status, saved.error = "FAILED", str(exc)
                db.commit()


@app.post("/api/files/", status_code=status.HTTP_201_CREATED)
def upload_file(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".zip", ".kml"}:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only .zip and .kml files are supported")
    file_id = str(uuid.uuid4())
    path = UPLOAD_DIR / f"{file_id}{suffix}"
    size = 0
    with path.open("wb") as output:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                path.unlink(missing_ok=True)
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File exceeds the 50 MB upload limit")
            output.write(chunk)
    if size == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    record = FileRecord(id=file_id, filename=file.filename or path.name, stored_path=str(path))
    with SessionLocal() as db:
        db.add(record)
        db.commit()
    process_file(record)
    with SessionLocal() as db:
        saved = db.get(FileRecord, file_id)
        if saved and saved.status == "FAILED":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Failed to process geospatial file: {saved.error}")
    return {"id": file_id, "status": "COMPLETED"}


def get_record(file_id: str) -> FileRecord:
    with SessionLocal() as db:
        record = db.get(FileRecord, file_id)
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File not found")
        return record


@app.get("/api/files/{file_id}/")
def file_info(file_id: str):
    record = get_record(file_id)
    return {
        "id": record.id,
        "filename": record.filename,
        "feature_count": record.feature_count,
        "crs": record.crs,
        "status": record.status,
        "error": record.error,
    }


@app.get("/api/files/{file_id}/measurements/")
def measurements(file_id: str):
    record = get_record(file_id)
    return {
        "id": record.id,
        "status": record.status,
        "measurements": record.measurements or [],
        "error": record.error,
    }


@app.exception_handler(ValueError)
def value_error_handler(_, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(zipfile.BadZipFile)
def bad_zip_handler(_, exc: zipfile.BadZipFile):
    return JSONResponse(status_code=422, content={"detail": f"Corrupt or invalid ZIP archive: {exc}"})

