import json
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.auth import create_access_token, get_current_user, verify_password
from app.config import settings
from app.db import Base, SessionLocal, engine, get_db
from app.geocode import search_address
from app.migrate import ensure_schema
from app.models import Marker, MarkerCategory, MarkerShape, User
from app.schemas import (
    GeocodeResult,
    LatLng,
    LoginRequest,
    MarkerCreate,
    MarkerOut,
    MarkerUpdate,
    TokenResponse,
    UserOut,
)
from app.seed import seed_data

app = FastAPI(title="Jinan Travel Map API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    db = SessionLocal()
    try:
        seed_data(db)
    finally:
        db.close()


def _parse_polygon(raw: Optional[str]) -> Optional[list[LatLng]]:
    if not raw:
        return None
    data = json.loads(raw)
    return [LatLng(**p) for p in data]


def marker_to_out(marker: Marker) -> MarkerOut:
    return MarkerOut(
        id=marker.id,
        user_id=marker.user_id,
        author_name=marker.user.display_name,
        category=marker.category,
        shape=marker.shape or MarkerShape.point,
        title=marker.title,
        description=marker.description,
        lat=marker.lat,
        lng=marker.lng,
        polygon=_parse_polygon(marker.polygon),
        created_at=marker.created_at,
        updated_at=marker.updated_at,
    )


def _centroid(points: list[LatLng]) -> tuple[float, float]:
    lat = sum(p.lat for p in points) / len(points)
    lng = sum(p.lng for p in points) / len(points)
    return lat, lng


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/geocode", response_model=list[GeocodeResult])
def geocode(
    q: str = Query(..., min_length=1, max_length=200),
    current_user: User = Depends(get_current_user),
) -> list[GeocodeResult]:
    _ = current_user
    try:
        return [GeocodeResult(**item) for item in search_address(q)]
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.email == body.email).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="이메일 또는 비밀번호가 올바르지 않습니다")
    return TokenResponse(access_token=create_access_token(user.id))


@app.get("/api/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@app.get("/api/markers", response_model=list[MarkerOut])
def list_markers(
    mine: bool = Query(False),
    category: Optional[MarkerCategory] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[MarkerOut]:
    q = db.query(Marker).join(User)
    if mine:
        q = q.filter(Marker.user_id == current_user.id)
    if category is not None:
        q = q.filter(Marker.category == category)
    markers = q.order_by(Marker.created_at.desc()).all()
    return [marker_to_out(m) for m in markers]


@app.post("/api/markers", response_model=MarkerOut, status_code=status.HTTP_201_CREATED)
def create_marker(
    body: MarkerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MarkerOut:
    lat, lng = body.lat, body.lng
    polygon_json: Optional[str] = None
    if body.shape == MarkerShape.polygon and body.polygon:
        lat, lng = _centroid(body.polygon)
        polygon_json = json.dumps([p.model_dump() for p in body.polygon])

    marker = Marker(
        user_id=current_user.id,
        category=body.category,
        shape=body.shape,
        title=body.title.strip(),
        description=body.description.strip(),
        lat=lat,
        lng=lng,
        polygon=polygon_json,
    )
    db.add(marker)
    db.commit()
    db.refresh(marker)
    return marker_to_out(marker)


@app.get("/api/markers/{marker_id}", response_model=MarkerOut)
def get_marker(
    marker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MarkerOut:
    marker = db.query(Marker).filter(Marker.id == marker_id).first()
    if marker is None:
        raise HTTPException(status_code=404, detail="마커를 찾을 수 없습니다")
    return marker_to_out(marker)


@app.patch("/api/markers/{marker_id}", response_model=MarkerOut)
def update_marker(
    marker_id: int,
    body: MarkerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MarkerOut:
    marker = db.query(Marker).filter(Marker.id == marker_id).first()
    if marker is None:
        raise HTTPException(status_code=404, detail="마커를 찾을 수 없습니다")
    if marker.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="본인 마커만 수정할 수 있습니다")

    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        data["title"] = data["title"].strip()
    if "description" in data and data["description"] is not None:
        data["description"] = data["description"].strip()
    if "polygon" in data:
        poly = data.pop("polygon")
        if poly is not None:
            points = [LatLng(**p) if isinstance(p, dict) else p for p in poly]
            data["polygon"] = json.dumps([p.model_dump() if isinstance(p, LatLng) else p for p in points])
            lat, lng = _centroid(points)
            data["lat"] = lat
            data["lng"] = lng

    for key, value in data.items():
        setattr(marker, key, value)

    db.commit()
    db.refresh(marker)
    return marker_to_out(marker)


@app.delete("/api/markers/{marker_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_marker(
    marker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    marker = db.query(Marker).filter(Marker.id == marker_id).first()
    if marker is None:
        raise HTTPException(status_code=404, detail="마커를 찾을 수 없습니다")
    if marker.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="본인 마커만 삭제할 수 있습니다")
    db.delete(marker)
    db.commit()


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if STATIC_DIR.is_dir():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    def spa_index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path in {"docs", "openapi.json", "redoc"}:
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
