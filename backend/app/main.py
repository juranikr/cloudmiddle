import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.admin_api import router as admin_router
from app.agent.runner import run_agent
from app.agent.tools import reconcile_proposal_tasks
from app.auth import create_access_token, get_admin_user, get_current_user, verify_password
from app.config import settings
from app.db import Base, SessionLocal, engine, get_db
from app.events import (
    changes_from_payload,
    diff_marker_fields,
    ensure_contributor,
    log_place_event,
    marker_field_snapshot,
    summary_for_changes,
)
from app.geocode import search_address
from app.messages import create_appeal
from app.migrate import ensure_schema
from app.models import (
    City,
    PlaceFavorite,
    Marker,
    MarkerCategory,
    MarkerShape,
    PlaceAppeal,
    PlaceContributor,
    PlaceEvent,
    PlaceEventAction,
    PlaceImage,
    PlaceInsight,
    PlaceNote,
    PlaceChain,
    TravelChatMessage,
    TravelChatWork,
    TravelPlan,
    TravelPlanDay,
    TravelPlanItem,
    TravelPlanMember,
    User,
    UserMessage,
    UserMessageKind,
)
from app.schemas import (
    AgentKnowledgeOut,
    FavoriteToggleOut,
    AgentRunResponse,
    AppealCreate,
    AppealOut,
    CityOut,
    GeocodeResult,
    ImageReorderRequest,
    ImageUploadRequest,
    ImageUploadResponse,
    LatLng,
    LoginRequest,
    MarkerCreate,
    MarkerOut,
    MarkerUpdate,
    PlaceEventOut,
    PlaceImageOut,
    PlaceInsightOut,
    PlaceNoteCreate,
    PlaceNoteUpdate,
    PlaceNoteOut,
    PlaceChainCreate,
    PlaceChainOut,
    ShareImportRequest,
    ShareImportResultOut,
    TokenResponse,
    UserMessageOut,
    UserOut,
    TravelChatMessageOut,
    TravelChatRequest,
    TravelChatResponse,
    TravelProfileOut,
    TravelPlanDayCreate,
    TravelPlanDayOut,
    TravelPlanDayUpdate,
    TravelPlanItemCreate,
    TravelPlanItemOut,
    TravelPlanItemUpdate,
    TravelPlanMemberOut,
    TravelPlanOut,
    TravelPlanUpdate,
)
from app.seed import seed_data
from app.share_import import import_share_text
from app import storage

app = FastAPI(title="Cloudmiddle China Travel Map API", version="0.3.0")
app.include_router(admin_router)

@app.middleware("http")
async def enforce_production_readonly(request: Request, call_next):
    """Expose diagnostics as reads only; the database also enforces this."""

    method = request.method.upper()
    login_read = method == "POST" and request.url.path == "/api/auth/login"
    if (
        settings.is_production_readonly
        and method not in {"GET", "HEAD", "OPTIONS"}
        and not login_read
    ):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": (
                    "This process is running in production_readonly diagnostic mode; "
                    "state-changing HTTP methods are disabled."
                )
            },
            headers={"X-Cloudmiddle-DB-Mode": settings.runtime_db_mode},
        )
    response = await call_next(request)
    response.headers["X-Cloudmiddle-DB-Mode"] = settings.runtime_db_mode
    return response


# Add CORS after the read-only guard so it is the outer middleware. Blocked
# cross-origin writes then remain a readable HTTP 503 instead of a browser-level
# CORS failure in the diagnostic UI.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|d232kzujcg4ufp\.cloudfront\.net)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    if settings.is_production_readonly:
        # Every normal startup action below can write. Diagnostics must observe
        # the deployed schema and never repair, seed, or reconcile it.
        return
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    db = SessionLocal()
    try:
        seed_data(db)
        for city_id, in db.query(City.id).filter(City.status == "active").all():
            reconcile_proposal_tasks(db, city_id=city_id)
    finally:
        db.close()


def _parse_polygon(raw: Optional[str]) -> Optional[list[LatLng]]:
    if not raw:
        return None
    data = json.loads(raw)
    return [LatLng(**p) for p in data]


def _centroid(points: list[LatLng]) -> tuple[float, float]:
    lat = sum(p.lat for p in points) / len(points)
    lng = sum(p.lng for p in points) / len(points)
    return lat, lng


def _validate_marker_links(
    db: Session,
    *,
    city_id: int,
    zone_id: Optional[int],
    chain_id: Optional[int],
    marker_id: Optional[int] = None,
) -> None:
    if zone_id is not None:
        zone = db.query(Marker).filter(Marker.id == zone_id, Marker.merged_into_id.is_(None)).first()
        if zone is None or zone.city_id != city_id or zone.shape != MarkerShape.polygon:
            raise HTTPException(status_code=400, detail="같은 도시의 유효한 구역만 선택할 수 있습니다")
        if marker_id is not None and zone.id == marker_id:
            raise HTTPException(status_code=400, detail="구역 자체를 소속 구역으로 선택할 수 없습니다")
    if chain_id is not None and db.query(PlaceChain.id).filter(PlaceChain.id == chain_id).first() is None:
        raise HTTPException(status_code=404, detail="체인을 찾을 수 없습니다")


def marker_to_out(marker: Marker, *, is_favorite: bool = False) -> MarkerOut:
    names: list[str] = []
    for c in marker.contributors or []:
        if c.user and c.user.display_name and c.user.display_name not in names:
            names.append(c.user.display_name)
    if marker.creator and marker.creator.display_name and marker.creator.display_name not in names:
        names.insert(0, marker.creator.display_name)
    images = [
        PlaceImageOut(
            id=img.id,
            url=storage.public_url(img.s3_key) if storage.s3_enabled() else "",
            sort_order=img.sort_order,
            group_key=img.group_key,
            content_type=img.content_type,
        )
        for img in sorted(marker.images or [], key=lambda x: x.sort_order)
    ]
    insights = [
        PlaceInsightOut(
            id=item.id,
            kind=item.kind,
            title=item.title,
            content=item.content or "",
            year_label=item.year_label or "",
            source_url=item.source_url or "",
            source_title=item.source_title or "",
            confidence=float(item.confidence or 0),
            verified_at=item.verified_at,
        )
        for item in sorted(marker.insights or [], key=lambda x: (x.sort_order, x.id))
    ]
    chain_name = ""
    if marker.chain:
        chain_name = marker.chain.name_local
        if marker.chain.name_ko:
            chain_name += f" ({marker.chain.name_ko})"
    return MarkerOut(
        id=marker.id,
        city_id=marker.city_id,
        user_id=marker.user_id,
        author_name=names[0] if names else (marker.creator.display_name if marker.creator else "공유"),
        contributor_names=names,
        category=marker.category,
        shape=marker.shape or MarkerShape.point,
        title=marker.title,
        description=marker.description,
        agent_context=marker.agent_context or "",
        lat=marker.lat,
        lng=marker.lng,
        polygon=_parse_polygon(marker.polygon),
        images=images,
        insights=insights,
        zone_id=marker.zone_id,
        zone_title=marker.zone.title if marker.zone else "",
        chain_id=marker.chain_id,
        chain_name=chain_name,
        branch_name=marker.branch_name or "",
        travel_role=marker.travel_role or "general",
        note_count=len(marker.notes or []),
        coordinate_source=marker.coordinate_source or "manual",
        coordinate_external_id=marker.coordinate_external_id or "",
        coordinate_query=marker.coordinate_query or "",
        coordinate_source_url=marker.coordinate_source_url or "",
        coordinate_confidence=marker.coordinate_confidence,
        coordinate_crs=marker.coordinate_crs or "WGS84",
        coordinate_verified_at=marker.coordinate_verified_at,
        is_agent_suggested=bool(marker.is_agent_suggested),
        is_favorite=is_favorite,
        created_at=marker.created_at,
        updated_at=marker.updated_at,
    )


def _load_place(db: Session, place_id: int) -> Optional[Marker]:
    return (
        db.query(Marker)
        .options(
            joinedload(Marker.creator),
            joinedload(Marker.contributors).joinedload(PlaceContributor.user),
            joinedload(Marker.images),
            joinedload(Marker.insights),
            joinedload(Marker.notes),
            joinedload(Marker.chain),
            joinedload(Marker.zone),
        )
        .filter(Marker.id == place_id, Marker.merged_into_id.is_(None))
        .first()
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "db_mode": settings.runtime_db_mode}


@app.get("/api/cities", response_model=list[CityOut])
def list_cities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CityOut]:
    _ = current_user
    counts = dict(
        db.query(Marker.city_id, func.count(Marker.id))
        .filter(Marker.merged_into_id.is_(None), Marker.shape == MarkerShape.point)
        .group_by(Marker.city_id)
        .all()
    )
    zone_counts = dict(
        db.query(Marker.city_id, func.count(Marker.id))
        .filter(Marker.merged_into_id.is_(None), Marker.shape == MarkerShape.polygon)
        .group_by(Marker.city_id)
        .all()
    )
    rows = db.query(City).filter(City.status == "active").order_by(City.sort_order, City.id).all()
    return [
        CityOut(
            id=city.id,
            slug=city.slug,
            name_ko=city.name_ko,
            name_local=city.name_local,
            country_code=city.country_code,
            center_lat=city.center_lat,
            center_lng=city.center_lng,
            default_zoom=city.default_zoom,
            search_viewbox=city.search_viewbox,
            status=city.status,
            place_count=int(counts.get(city.id, 0)),
            zone_count=int(zone_counts.get(city.id, 0)),
        )
        for city in rows
    ]


@app.get("/api/geocode", response_model=list[GeocodeResult])
def geocode(
    q: str = Query(..., min_length=1, max_length=200),
    city_id: int = Query(1, gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[GeocodeResult]:
    _ = current_user
    city = db.query(City).filter(City.id == city_id, City.status == "active").first()
    if city is None:
        raise HTTPException(status_code=404, detail="도시를 찾을 수 없습니다")
    local_rows = (
        db.query(Marker)
        .filter(Marker.city_id == city.id, Marker.merged_into_id.is_(None))
        .order_by(Marker.updated_at.desc())
        .all()
    )
    local_candidates = [
        {
            "id": marker.id,
            "title": marker.title,
            "description": marker.description,
            "lat": marker.lat,
            "lng": marker.lng,
            "type": marker.category.value,
        }
        for marker in local_rows
    ]
    try:
        return [
            GeocodeResult(**item)
            for item in search_address(
                q,
                viewbox=city.search_viewbox,
                city_name=city.name_local,
                city_context=city.search_context,
                city_slug=city.slug,
                city_name_ko=city.name_ko,
                country_code=city.country_code,
                local_candidates=local_candidates,
                include_display_only=True,
                arcgis_api_key=settings.arcgis_api_key,
            )
        ]
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/import/share", response_model=ShareImportResultOut)
def import_share(
    body: ShareImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShareImportResultOut:
    _ = current_user
    city = db.query(City).filter(City.id == body.city_id, City.status == "active").first()
    if city is None:
        raise HTTPException(status_code=404, detail="도시를 찾을 수 없습니다")
    try:
        result = import_share_text(
            body.text,
            preferred_source=body.source,
            city_name=city.name_local,
            city_context=city.search_context,
            viewbox=city.search_viewbox,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ShareImportResultOut(
        source=result.source,
        title=result.title,
        description=result.description,
        address=result.address,
        source_url=result.source_url,
        lat=result.lat,
        lng=result.lng,
        category_hint=result.category_hint,
        needs_map_pick=result.needs_map_pick,
        note=result.note,
    )


@app.post("/api/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.email == body.email).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="이메일 또는 비밀번호가 올바르지 않습니다")
    return TokenResponse(access_token=create_access_token(user.id))


@app.get("/api/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(
        id=current_user.id,
        email=current_user.email,
        display_name=current_user.display_name,
        created_at=current_user.created_at,
        is_admin=current_user.email.lower() in settings.admin_email_list,
    )


@app.get("/api/markers", response_model=list[MarkerOut])
def list_markers(
    city_id: int = Query(1, gt=0),
    category: Optional[MarkerCategory] = Query(None),
    favorites_only: bool = Query(False),
    agent_suggested_only: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[MarkerOut]:
    fav_ids = {
        r.place_id
        for r in db.query(PlaceFavorite.place_id).filter(PlaceFavorite.user_id == current_user.id).all()
    }
    q = (
        db.query(Marker)
        .options(
            joinedload(Marker.creator),
            joinedload(Marker.contributors).joinedload(PlaceContributor.user),
            joinedload(Marker.images),
            joinedload(Marker.insights),
            joinedload(Marker.notes),
            joinedload(Marker.chain),
            joinedload(Marker.zone),
        )
        .filter(Marker.merged_into_id.is_(None))
        .filter(Marker.city_id == city_id)
    )
    if category is not None:
        q = q.filter(Marker.category == category)
    if favorites_only:
        if not fav_ids:
            return []
        q = q.filter(Marker.id.in_(fav_ids))
    if agent_suggested_only:
        q = q.filter(Marker.is_agent_suggested.is_(True))
    markers = q.order_by(Marker.created_at.desc()).all()
    return [marker_to_out(m, is_favorite=m.id in fav_ids) for m in markers]


@app.post("/api/markers", response_model=MarkerOut, status_code=status.HTTP_201_CREATED)
def create_marker(
    body: MarkerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MarkerOut:
    if db.query(City.id).filter(City.id == body.city_id, City.status == "active").first() is None:
        raise HTTPException(status_code=404, detail="도시를 찾을 수 없습니다")
    _validate_marker_links(
        db, city_id=body.city_id, zone_id=body.zone_id, chain_id=body.chain_id
    )
    lat, lng = body.lat, body.lng
    polygon_json: Optional[str] = None
    if body.shape == MarkerShape.polygon and body.polygon:
        lat, lng = _centroid(body.polygon)
        polygon_json = json.dumps([p.model_dump() for p in body.polygon])

    marker = Marker(
        user_id=current_user.id,
        city_id=body.city_id,
        category=body.category,
        shape=body.shape,
        title=body.title.strip(),
        description=body.description.strip(),
        lat=lat,
        lng=lng,
        polygon=polygon_json,
        coordinate_source=body.coordinate_source or "manual",
        coordinate_external_id=body.coordinate_external_id,
        coordinate_query=body.coordinate_query,
        coordinate_source_url=body.coordinate_source_url,
        coordinate_confidence=body.coordinate_confidence,
        coordinate_crs=body.coordinate_crs or "WGS84",
        zone_id=body.zone_id if body.shape == MarkerShape.point else None,
        chain_id=body.chain_id if body.shape == MarkerShape.point else None,
        branch_name=body.branch_name.strip() if body.shape == MarkerShape.point else "",
        travel_role=body.travel_role if body.shape == MarkerShape.point else "general",
    )
    db.add(marker)
    db.flush()
    ensure_contributor(db, marker.id, current_user.id)
    log_place_event(
        db,
        place_id=marker.id,
        city_id=marker.city_id,
        user=current_user,
        action=PlaceEventAction.create,
        summary=f"장소 추가: {marker.title}",
        payload={
            "place_id": marker.id,
            "category": marker.category.value,
            "lat": lat,
            "lng": lng,
            "after": marker_field_snapshot(marker),
        },
    )
    db.commit()
    marker = _load_place(db, marker.id)
    assert marker is not None
    return marker_to_out(marker)


@app.get("/api/markers/{marker_id}", response_model=MarkerOut)
def get_marker(
    marker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MarkerOut:
    _ = current_user
    marker = _load_place(db, marker_id)
    if marker is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    fav = db.query(PlaceFavorite).filter(PlaceFavorite.user_id == current_user.id, PlaceFavorite.place_id == marker.id).first()
    return marker_to_out(marker, is_favorite=fav is not None)


def _note_out(note: PlaceNote, current_user: User) -> PlaceNoteOut:
    return PlaceNoteOut(
        id=note.id,
        place_id=note.place_id,
        user_id=note.user_id,
        author_name=note.user.display_name if note.user else "사용자",
        body=note.body,
        visibility=note.visibility,
        is_mine=note.user_id == current_user.id,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


@app.get("/api/markers/{marker_id}/notes", response_model=list[PlaceNoteOut])
def list_place_notes(
    marker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[PlaceNoteOut]:
    if db.query(Marker.id).filter(Marker.id == marker_id, Marker.merged_into_id.is_(None)).first() is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    rows = (
        db.query(PlaceNote)
        .options(joinedload(PlaceNote.user))
        .filter(
            PlaceNote.place_id == marker_id,
            or_(PlaceNote.visibility == "shared", PlaceNote.user_id == current_user.id),
        )
        .order_by(PlaceNote.created_at.asc())
        .all()
    )
    return [_note_out(row, current_user) for row in rows]


@app.post("/api/markers/{marker_id}/notes", response_model=PlaceNoteOut, status_code=status.HTTP_201_CREATED)
def create_place_note(
    marker_id: int,
    body: PlaceNoteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlaceNoteOut:
    if db.query(Marker.id).filter(Marker.id == marker_id, Marker.merged_into_id.is_(None)).first() is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    row = PlaceNote(
        place_id=marker_id,
        user_id=current_user.id,
        body=body.body.strip(),
        visibility=body.visibility,
    )
    db.add(row)
    db.commit()
    row = db.query(PlaceNote).options(joinedload(PlaceNote.user)).filter(PlaceNote.id == row.id).one()
    return _note_out(row, current_user)


@app.patch("/api/notes/{note_id}", response_model=PlaceNoteOut)
def update_place_note(
    note_id: int,
    body: PlaceNoteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlaceNoteOut:
    row = db.query(PlaceNote).filter(PlaceNote.id == note_id, PlaceNote.user_id == current_user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="수정할 메모를 찾을 수 없습니다")
    row.body = body.body.strip()
    if body.visibility is not None:
        row.visibility = body.visibility
    db.commit()
    row = db.query(PlaceNote).options(joinedload(PlaceNote.user)).filter(PlaceNote.id == note_id).one()
    return _note_out(row, current_user)


@app.delete("/api/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_place_note(
    note_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    row = db.query(PlaceNote).filter(PlaceNote.id == note_id, PlaceNote.user_id == current_user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="삭제할 메모를 찾을 수 없습니다")
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _chain_out(row: PlaceChain, branch_count: int = 0) -> PlaceChainOut:
    try:
        aliases = json.loads(row.aliases or "[]")
    except json.JSONDecodeError:
        aliases = []
    return PlaceChainOut(
        id=row.id,
        name_local=row.name_local,
        name_ko=row.name_ko or "",
        category=row.category or "other",
        aliases=[str(item) for item in aliases if str(item).strip()],
        description=row.description or "",
        branch_count=int(branch_count),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@app.get("/api/chains", response_model=list[PlaceChainOut])
def list_place_chains(
    city_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[PlaceChainOut]:
    _ = current_user
    count_query = db.query(Marker.chain_id, func.count(Marker.id)).filter(
        Marker.chain_id.is_not(None), Marker.merged_into_id.is_(None)
    )
    if city_id is not None:
        count_query = count_query.filter(Marker.city_id == city_id)
    counts = dict(count_query.group_by(Marker.chain_id).all())
    rows = db.query(PlaceChain).order_by(PlaceChain.name_local.asc()).all()
    return [_chain_out(row, counts.get(row.id, 0)) for row in rows if city_id is None or counts.get(row.id, 0)]


@app.post("/api/chains", response_model=PlaceChainOut, status_code=status.HTTP_201_CREATED)
def create_place_chain(
    body: PlaceChainCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlaceChainOut:
    existing = db.query(PlaceChain).filter(
        func.lower(PlaceChain.name_local) == body.name_local.strip().lower()
    ).first()
    if existing is not None:
        return _chain_out(existing, len(existing.branches or []))
    row = PlaceChain(
        name_local=body.name_local.strip(),
        name_ko=body.name_ko.strip(),
        category=body.category,
        aliases=json.dumps([item.strip() for item in body.aliases if item.strip()], ensure_ascii=False),
        description=body.description.strip(),
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _chain_out(row)


@app.get("/api/markers/{marker_id}/events", response_model=list[PlaceEventOut])
def list_marker_events(
    marker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[PlaceEventOut]:
    _ = current_user
    marker = db.query(Marker).filter(Marker.id == marker_id).first()
    if marker is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    rows = (
        db.query(PlaceEvent)
        .filter(PlaceEvent.place_id == marker_id)
        .order_by(PlaceEvent.created_at.desc())
        .limit(100)
        .all()
    )
    user_ids = {e.user_id for e in rows if e.user_id}
    names: dict[int, str] = {}
    if user_ids:
        for u in db.query(User).filter(User.id.in_(user_ids)).all():
            names[u.id] = u.display_name
    out: list[PlaceEventOut] = []
    for e in rows:
        if e.actor == "agent":
            actor_name = "에이전트"
        elif e.actor == "system":
            actor_name = "시스템"
        else:
            actor_name = names.get(e.user_id or -1, "사용자")
        try:
            payload = json.loads(e.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        changes = changes_from_payload(payload)
        # merge 등: 요약용 힌트만
        if not changes and e.action == PlaceEventAction.merge:
            changes = [
                {
                    "field": "merge",
                    "before": payload.get("source_id"),
                    "after": payload.get("target_id"),
                }
            ]
        out.append(
            PlaceEventOut(
                id=e.id,
                city_id=e.city_id,
                place_id=e.place_id,
                user_id=e.user_id,
                actor_name=actor_name,
                actor=e.actor,
                action=e.action.value if e.action else "",
                summary=e.summary or "",
                changes=changes,
                groq_read=e.groq_read_at is not None,
                created_at=e.created_at,
            )
        )
    return out


@app.patch("/api/markers/{marker_id}", response_model=MarkerOut)
def update_marker(
    marker_id: int,
    body: MarkerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MarkerOut:
    marker = db.query(Marker).filter(Marker.id == marker_id, Marker.merged_into_id.is_(None)).first()
    if marker is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")

    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        data["title"] = data["title"].strip()
    if "description" in data and data["description"] is not None:
        data["description"] = data["description"].strip()
    if "branch_name" in data and data["branch_name"] is not None:
        data["branch_name"] = data["branch_name"].strip()
    next_zone_id = data.get("zone_id", marker.zone_id)
    next_chain_id = data.get("chain_id", marker.chain_id)
    if marker.shape == MarkerShape.polygon and (next_zone_id is not None or next_chain_id is not None):
        raise HTTPException(status_code=400, detail="구역은 다른 구역이나 체인에 소속될 수 없습니다")
    _validate_marker_links(
        db,
        city_id=marker.city_id,
        zone_id=next_zone_id,
        chain_id=next_chain_id,
        marker_id=marker.id,
    )
    if "polygon" in data:
        poly = data.pop("polygon")
        if poly is not None:
            points = [LatLng(**p) if isinstance(p, dict) else p for p in poly]
            data["polygon"] = json.dumps(
                [p.model_dump() if isinstance(p, LatLng) else p for p in points]
            )
            lat, lng = _centroid(points)
            data["lat"] = lat
            data["lng"] = lng

    before = marker_field_snapshot(marker)
    for key, value in data.items():
        setattr(marker, key, value)
    after = marker_field_snapshot(marker)
    changes = diff_marker_fields(before, after)

    ensure_contributor(db, marker.id, current_user.id)
    log_place_event(
        db,
        place_id=marker.id,
        city_id=marker.city_id,
        user=current_user,
        action=PlaceEventAction.update,
        summary=summary_for_changes("장소 수정", changes) if changes else f"장소 수정: {marker.title}",
        payload={
            "before": before,
            "after": after,
            "changes": changes,
            "fields": [c["field"] for c in changes],
        },
    )
    db.commit()
    marker = _load_place(db, marker_id)
    assert marker is not None
    return marker_to_out(marker)


@app.delete("/api/markers/{marker_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_marker(
    marker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    marker = db.query(Marker).filter(Marker.id == marker_id, Marker.merged_into_id.is_(None)).first()
    if marker is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    before = marker_field_snapshot(marker)
    log_place_event(
        db,
        place_id=marker.id,
        city_id=marker.city_id,
        user=current_user,
        action=PlaceEventAction.delete,
        summary=f"장소 삭제: {marker.title}",
        payload={
            "place_id": marker.id,
            "title": marker.title,
            "before": before,
        },
    )
    if marker.shape == MarkerShape.polygon:
        db.query(Marker).filter(Marker.zone_id == marker.id).update(
            {Marker.zone_id: None}, synchronize_session=False
        )
    db.delete(marker)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/markers/{marker_id}/images/presign", response_model=ImageUploadResponse)
def presign_image(
    marker_id: int,
    body: ImageUploadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ImageUploadResponse:
    if not storage.s3_enabled():
        raise HTTPException(status_code=501, detail="S3 이미지 업로드가 아직 설정되지 않았습니다")
    marker = db.query(Marker).filter(Marker.id == marker_id, Marker.merged_into_id.is_(None)).first()
    if marker is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    ct = body.content_type or "image/jpeg"
    if not ct.startswith("image/"):
        raise HTTPException(status_code=400, detail="이미지 파일만 업로드할 수 있습니다")
    key = storage.build_object_key(marker_id, body.filename, ct)
    max_order = max([i.sort_order for i in marker.images], default=-1)
    img = PlaceImage(
        place_id=marker_id,
        s3_key=key,
        content_type=ct,
        sort_order=max_order + 1,
        uploaded_by_user_id=current_user.id,
    )
    db.add(img)
    db.flush()
    ensure_contributor(db, marker_id, current_user.id)
    changes = [{"field": "image_id", "before": None, "after": img.id}]
    log_place_event(
        db,
        place_id=marker_id,
        city_id=marker.city_id,
        user=current_user,
        action=PlaceEventAction.image_add,
        summary="이미지 추가",
        payload={"image_id": img.id, "s3_key": key, "changes": changes, "fields": ["image_id"]},
    )
    db.commit()
    return ImageUploadResponse(
        image_id=img.id,
        upload_url=storage.presign_put(key, ct),
        public_url=storage.public_url(key),
        s3_key=key,
    )


@app.put("/api/markers/{marker_id}/images/order", response_model=MarkerOut)
def reorder_images(
    marker_id: int,
    body: ImageReorderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MarkerOut:
    marker = db.query(Marker).filter(Marker.id == marker_id, Marker.merged_into_id.is_(None)).first()
    if marker is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    before_ids = [i.id for i in sorted(marker.images, key=lambda x: x.sort_order)]
    by_id = {i.id: i for i in marker.images}
    for idx, iid in enumerate(body.image_ids):
        if iid in by_id:
            by_id[iid].sort_order = idx
    ensure_contributor(db, marker_id, current_user.id)
    changes = [{"field": "image_ids", "before": before_ids, "after": list(body.image_ids)}]
    log_place_event(
        db,
        place_id=marker_id,
        city_id=marker.city_id,
        user=current_user,
        action=PlaceEventAction.image_reorder,
        summary="이미지 순서 변경",
        payload={
            "image_ids": body.image_ids,
            "before": {"image_ids": before_ids},
            "after": {"image_ids": list(body.image_ids)},
            "changes": changes,
            "fields": ["image_ids"],
        },
    )
    db.commit()
    marker = _load_place(db, marker_id)
    assert marker is not None
    return marker_to_out(marker)


@app.post("/api/agent/run", response_model=AgentRunResponse)
def agent_run(
    city_id: int = Query(..., gt=0),
    research: bool = Query(False),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AgentRunResponse:
    """하위 호환. 관리자만 실행 가능 — `/api/admin/agent/run` 권장."""
    _ = admin
    result = run_agent(db, city_id=city_id, autonomous_research=research)
    return AgentRunResponse(**result)


def _message_to_out(msg: UserMessage) -> UserMessageOut:
    can_appeal = msg.kind in (UserMessageKind.agent_merge, UserMessageKind.agent_create) and bool(
        msg.place_id
    )
    return UserMessageOut(
        id=msg.id,
        place_id=msg.place_id,
        kind=msg.kind.value if msg.kind else "system",
        title=msg.title,
        body=msg.body,
        read_at=msg.read_at,
        created_at=msg.created_at,
        can_appeal=can_appeal,
    )




@app.get("/api/favorites", response_model=list[MarkerOut])
def list_favorites(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[MarkerOut]:
    fav_ids = [
        r.place_id
        for r in db.query(PlaceFavorite)
        .filter(PlaceFavorite.user_id == current_user.id)
        .order_by(PlaceFavorite.created_at.desc())
        .all()
    ]
    if not fav_ids:
        return []
    markers = (
        db.query(Marker)
        .options(
            joinedload(Marker.creator),
            joinedload(Marker.contributors).joinedload(PlaceContributor.user),
            joinedload(Marker.images),
        )
        .filter(Marker.id.in_(fav_ids), Marker.merged_into_id.is_(None))
        .all()
    )
    by_id = {m.id: m for m in markers}
    return [marker_to_out(by_id[i], is_favorite=True) for i in fav_ids if i in by_id]


@app.post("/api/favorites/{place_id}", response_model=FavoriteToggleOut)
def add_favorite(
    place_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FavoriteToggleOut:
    place = db.query(Marker).filter(Marker.id == place_id, Marker.merged_into_id.is_(None)).first()
    if not place:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    exists = (
        db.query(PlaceFavorite)
        .filter(PlaceFavorite.user_id == current_user.id, PlaceFavorite.place_id == place_id)
        .first()
    )
    if exists is None:
        db.add(PlaceFavorite(user_id=current_user.id, place_id=place_id))
        db.commit()
    return FavoriteToggleOut(place_id=place_id, is_favorite=True)


@app.delete("/api/favorites/{place_id}", response_model=FavoriteToggleOut)
def remove_favorite(
    place_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FavoriteToggleOut:
    row = (
        db.query(PlaceFavorite)
        .filter(PlaceFavorite.user_id == current_user.id, PlaceFavorite.place_id == place_id)
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return FavoriteToggleOut(place_id=place_id, is_favorite=False)

@app.get("/api/messages", response_model=list[UserMessageOut])
def list_messages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[UserMessageOut]:
    rows = (
        db.query(UserMessage)
        .filter(UserMessage.user_id == current_user.id)
        .order_by(UserMessage.created_at.desc())
        .limit(100)
        .all()
    )
    return [_message_to_out(m) for m in rows]


@app.get("/api/messages/unread-count")
def messages_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    n = (
        db.query(UserMessage)
        .filter(UserMessage.user_id == current_user.id, UserMessage.read_at.is_(None))
        .count()
    )
    return {"count": n}


@app.post("/api/messages/{message_id}/read", response_model=UserMessageOut)
def read_message(
    message_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UserMessageOut:
    from datetime import datetime, timezone

    msg = (
        db.query(UserMessage)
        .filter(UserMessage.id == message_id, UserMessage.user_id == current_user.id)
        .first()
    )
    if msg is None:
        raise HTTPException(status_code=404, detail="메시지를 찾을 수 없습니다")
    if msg.read_at is None:
        msg.read_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(msg)
    return _message_to_out(msg)


@app.post("/api/messages/read-all")
def read_all_messages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    rows = (
        db.query(UserMessage)
        .filter(UserMessage.user_id == current_user.id, UserMessage.read_at.is_(None))
        .all()
    )
    for m in rows:
        m.read_at = now
    db.commit()
    return {"marked": len(rows)}


@app.post("/api/appeals", response_model=AppealOut, status_code=status.HTTP_201_CREATED)
def post_appeal(
    body: AppealCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AppealOut:
    try:
        appeal = create_appeal(
            db,
            user=current_user,
            place_id=body.place_id,
            body=body.body,
            message_id=body.message_id,
        )
        db.commit()
        db.refresh(appeal)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AppealOut(
        id=appeal.id,
        place_id=appeal.place_id,
        body=appeal.body,
        status=appeal.status.value,
        agent_note=appeal.agent_note or "",
        created_at=appeal.created_at,
        resolved_at=appeal.resolved_at,
    )


@app.get("/api/appeals/mine", response_model=list[AppealOut])
def my_appeals(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AppealOut]:
    rows = (
        db.query(PlaceAppeal)
        .filter(PlaceAppeal.user_id == current_user.id)
        .order_by(PlaceAppeal.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        AppealOut(
            id=a.id,
            place_id=a.place_id,
            body=a.body,
            status=a.status.value,
            agent_note=a.agent_note or "",
            created_at=a.created_at,
            resolved_at=a.resolved_at,
        )
        for a in rows
    ]


def _plan_item_to_out(row: TravelPlanItem, *, favorite_ids: set[int] | None = None) -> TravelPlanItemOut:
    assert row.plan_id is not None
    return TravelPlanItemOut(
        id=row.id,
        plan_id=row.plan_id,
        plan_day_id=row.plan_day_id,
        city_id=row.city_id,
        place_id=row.place_id,
        created_by_user_id=row.user_id,
        creator_name=row.creator.display_name if row.creator else "사용자",
        day=row.day,
        slot=row.slot,
        start_time=row.start_time,
        end_time=row.end_time,
        sort_order=row.sort_order,
        note=row.note or "",
        legacy_day=row.day if row.plan_day_id is None else None,
        legacy_slot=row.slot if row.plan_day_id is None else "",
        place=marker_to_out(row.place, is_favorite=row.place_id in (favorite_ids or set())),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _load_travel_plan(db: Session, plan_id: int) -> Optional[TravelPlan]:
    return (
        db.query(TravelPlan)
        .options(
            joinedload(TravelPlan.owner),
            joinedload(TravelPlan.members).joinedload(TravelPlanMember.user),
            joinedload(TravelPlan.days).joinedload(TravelPlanDay.items).joinedload(TravelPlanItem.creator),
            joinedload(TravelPlan.days).joinedload(TravelPlanDay.items).joinedload(TravelPlanItem.place),
            joinedload(TravelPlan.items).joinedload(TravelPlanItem.creator),
            joinedload(TravelPlan.items).joinedload(TravelPlanItem.place),
        )
        .filter(TravelPlan.id == plan_id)
        .first()
    )


def _plan_member_role(plan: TravelPlan, user_id: int) -> str:
    if plan.owner_user_id == user_id:
        return "owner"
    for member in plan.members:
        if member.user_id == user_id and member.invitation_status == "accepted":
            return member.role
    return ""


def _can_view_plan(plan: TravelPlan, user_id: int) -> bool:
    return plan.visibility in {"city_shared", "public"} or bool(_plan_member_role(plan, user_id))


def _can_edit_plan(plan: TravelPlan, user_id: int) -> bool:
    if plan.visibility == "city_shared":
        return True
    return _plan_member_role(plan, user_id) in {"owner", "editor"}


def _can_manage_plan(plan: TravelPlan, user_id: int) -> bool:
    return _plan_member_role(plan, user_id) == "owner"


def _ensure_shared_member(db: Session, plan: TravelPlan, user: User) -> None:
    if plan.owner_user_id is None:
        plan.owner_user_id = user.id
    member = next((item for item in plan.members if item.user_id == user.id), None)
    expected_role = "owner" if plan.owner_user_id == user.id else "editor"
    if member is None:
        db.add(
            TravelPlanMember(
                plan_id=plan.id,
                user_id=user.id,
                role=expected_role,
                invitation_status="accepted",
                invited_by_user_id=plan.owner_user_id,
            )
        )
        db.commit()
    elif member.role != expected_role or member.invitation_status != "accepted":
        member.role = expected_role
        member.invitation_status = "accepted"
        db.commit()


def _get_city_shared_plan(db: Session, city_id: int, current_user: User) -> TravelPlan:
    city = db.query(City).filter(City.id == city_id, City.status == "active").first()
    if city is None:
        raise HTTPException(status_code=404, detail="도시를 찾을 수 없습니다")
    plan = (
        db.query(TravelPlan)
        .filter(
            TravelPlan.city_id == city_id,
            TravelPlan.visibility == "city_shared",
            TravelPlan.status != "archived",
        )
        .order_by(TravelPlan.id)
        .first()
    )
    if plan is None:
        plan = TravelPlan(
            city_id=city.id,
            owner_user_id=current_user.id,
            title=f"{city.name_ko} 함께 만드는 여행",
            description="이 도시를 여행하는 사용자들이 함께 편집하는 공용 일정표입니다.",
            visibility="city_shared",
            status="published",
            timezone="Asia/Shanghai",
            published_at=datetime.now(timezone.utc),
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    _ensure_shared_member(db, plan, current_user)
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    return plan


def _sync_plan_date_bounds(db: Session, plan: TravelPlan) -> None:
    start_date, end_date = (
        db.query(func.min(TravelPlanDay.calendar_date), func.max(TravelPlanDay.calendar_date))
        .filter(TravelPlanDay.plan_id == plan.id)
        .one()
    )
    plan.start_date = start_date
    plan.end_date = end_date
    plan.updated_at = datetime.now(timezone.utc)


def _travel_plan_to_out(
    db: Session,
    plan: TravelPlan,
    current_user: User,
) -> TravelPlanOut:
    if not _can_view_plan(plan, current_user.id):
        raise HTTPException(status_code=404, detail="일정표를 찾을 수 없습니다")
    favorite_ids = {
        item.place_id
        for item in db.query(PlaceFavorite.place_id).filter(PlaceFavorite.user_id == current_user.id).all()
    }

    def item_key(item: TravelPlanItem) -> tuple[bool, object, int, int]:
        return (item.start_time is None, item.start_time or "", item.sort_order, item.id)

    day_rows = []
    for day in sorted(plan.days, key=lambda item: (item.calendar_date, item.sort_order, item.id)):
        day_rows.append(
            TravelPlanDayOut(
                id=day.id,
                plan_id=day.plan_id,
                calendar_date=day.calendar_date,
                title=day.title or "",
                note=day.note or "",
                sort_order=day.sort_order,
                created_by_user_id=day.created_by_user_id,
                items=[
                    _plan_item_to_out(item, favorite_ids=favorite_ids)
                    for item in sorted(day.items, key=item_key)
                ],
                created_at=day.created_at,
                updated_at=day.updated_at,
            )
        )
    unscheduled = [item for item in plan.items if item.plan_day_id is None]
    return TravelPlanOut(
        id=plan.id,
        city_id=plan.city_id,
        owner_user_id=plan.owner_user_id,
        owner_name=plan.owner.display_name if plan.owner else "",
        title=plan.title,
        description=plan.description or "",
        visibility=plan.visibility,
        status=plan.status,
        timezone=plan.timezone,
        cover_image_url=plan.cover_image_url or "",
        start_date=plan.start_date,
        end_date=plan.end_date,
        can_edit=_can_edit_plan(plan, current_user.id),
        can_manage=_can_manage_plan(plan, current_user.id),
        members=[
            TravelPlanMemberOut(
                user_id=member.user_id,
                display_name=member.user.display_name if member.user else "사용자",
                role=member.role,
                invitation_status=member.invitation_status,
            )
            for member in sorted(plan.members, key=lambda item: (item.role != "owner", item.id))
        ],
        days=day_rows,
        unscheduled_items=[
            _plan_item_to_out(item, favorite_ids=favorite_ids)
            for item in sorted(unscheduled, key=lambda item: (item.day, item.sort_order, item.id))
        ],
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


@app.get("/api/travel-plans/shared", response_model=TravelPlanOut)
def get_shared_travel_plan(
    city_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanOut:
    plan = _get_city_shared_plan(db, city_id, current_user)
    return _travel_plan_to_out(db, plan, current_user)


@app.get("/api/travel-plans/{plan_id}", response_model=TravelPlanOut)
def get_travel_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanOut:
    plan = _load_travel_plan(db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="일정표를 찾을 수 없습니다")
    return _travel_plan_to_out(db, plan, current_user)


@app.patch("/api/travel-plans/{plan_id}", response_model=TravelPlanOut)
def update_travel_plan(
    plan_id: int,
    body: TravelPlanUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanOut:
    plan = _load_travel_plan(db, plan_id)
    if plan is None or not _can_manage_plan(plan, current_user.id):
        raise HTTPException(status_code=404, detail="관리할 일정표를 찾을 수 없습니다")
    values = body.model_dump(exclude_unset=True)
    if values.get("start_date") and values.get("end_date") and values["end_date"] < values["start_date"]:
        raise HTTPException(status_code=422, detail="종료일은 시작일보다 빠를 수 없습니다")
    for key, value in values.items():
        setattr(plan, key, value.strip() if isinstance(value, str) else value)
    if plan.status == "published" and plan.published_at is None:
        plan.published_at = datetime.now(timezone.utc)
    db.commit()
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    return _travel_plan_to_out(db, plan, current_user)


@app.post(
    "/api/travel-plans/{plan_id}/days",
    response_model=TravelPlanOut,
    status_code=status.HTTP_201_CREATED,
)
def create_travel_plan_day(
    plan_id: int,
    body: TravelPlanDayCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanOut:
    plan = _load_travel_plan(db, plan_id)
    if plan is None or not _can_edit_plan(plan, current_user.id):
        raise HTTPException(status_code=404, detail="편집할 일정표를 찾을 수 없습니다")
    max_order = db.query(func.max(TravelPlanDay.sort_order)).filter(TravelPlanDay.plan_id == plan.id).scalar() or 0
    db.add(
        TravelPlanDay(
            plan_id=plan.id,
            calendar_date=body.calendar_date,
            title=body.title.strip(),
            note=body.note.strip(),
            sort_order=max_order + 10,
            created_by_user_id=current_user.id,
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 일정표에 추가된 날짜입니다") from exc
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    _sync_plan_date_bounds(db, plan)
    db.commit()
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    return _travel_plan_to_out(db, plan, current_user)


@app.patch("/api/travel-plan-days/{day_id}", response_model=TravelPlanOut)
def update_travel_plan_day(
    day_id: int,
    body: TravelPlanDayUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanOut:
    day = db.query(TravelPlanDay).filter(TravelPlanDay.id == day_id).first()
    if day is None:
        raise HTTPException(status_code=404, detail="일정 날짜를 찾을 수 없습니다")
    plan = _load_travel_plan(db, day.plan_id)
    if plan is None or not _can_edit_plan(plan, current_user.id):
        raise HTTPException(status_code=404, detail="편집할 일정표를 찾을 수 없습니다")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(day, key, value.strip() if isinstance(value, str) else value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 일정표에 추가된 날짜입니다") from exc
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    _sync_plan_date_bounds(db, plan)
    db.commit()
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    return _travel_plan_to_out(db, plan, current_user)


@app.delete("/api/travel-plan-days/{day_id}", response_model=TravelPlanOut)
def delete_travel_plan_day(
    day_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanOut:
    day = db.query(TravelPlanDay).filter(TravelPlanDay.id == day_id).first()
    if day is None:
        raise HTTPException(status_code=404, detail="일정 날짜를 찾을 수 없습니다")
    plan = _load_travel_plan(db, day.plan_id)
    if plan is None or not _can_edit_plan(plan, current_user.id):
        raise HTTPException(status_code=404, detail="편집할 일정표를 찾을 수 없습니다")
    for item in day.items:
        item.plan_day_id = None
    db.delete(day)
    db.flush()
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    _sync_plan_date_bounds(db, plan)
    db.commit()
    plan = _load_travel_plan(db, plan.id)
    assert plan is not None
    return _travel_plan_to_out(db, plan, current_user)


def _create_plan_item_for_plan(
    db: Session,
    plan: TravelPlan,
    body: TravelPlanItemCreate,
    current_user: User,
) -> TravelPlanItemOut:
    if not _can_edit_plan(plan, current_user.id):
        raise HTTPException(status_code=404, detail="편집할 일정표를 찾을 수 없습니다")
    place = _load_place(db, body.place_id)
    if place is None or place.city_id != plan.city_id or place.shape != MarkerShape.point:
        raise HTTPException(status_code=404, detail="이 도시의 장소를 찾을 수 없습니다")
    day = None
    if body.plan_day_id is not None:
        day = db.query(TravelPlanDay).filter(
            TravelPlanDay.id == body.plan_day_id,
            TravelPlanDay.plan_id == plan.id,
        ).first()
        if day is None:
            raise HTTPException(status_code=404, detail="이 일정표의 날짜를 찾을 수 없습니다")
    duplicate = db.query(TravelPlanItem.id).filter(
        TravelPlanItem.plan_id == plan.id,
        TravelPlanItem.place_id == body.place_id,
        TravelPlanItem.plan_day_id == body.plan_day_id,
        TravelPlanItem.start_time == body.start_time,
    ).first()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="같은 날짜와 시간에 이미 담긴 장소입니다")
    max_order = (
        db.query(func.max(TravelPlanItem.sort_order))
        .filter(TravelPlanItem.plan_id == plan.id, TravelPlanItem.plan_day_id == body.plan_day_id)
        .scalar()
        or 0
    )
    row = TravelPlanItem(
        plan_id=plan.id,
        plan_day_id=day.id if day else None,
        user_id=current_user.id,
        city_id=plan.city_id,
        place_id=body.place_id,
        start_time=body.start_time,
        end_time=body.end_time,
        sort_order=max_order + 10,
        note=body.note.strip(),
    )
    plan.updated_at = datetime.now(timezone.utc)
    db.add(row)
    db.commit()
    row = (
        db.query(TravelPlanItem)
        .options(joinedload(TravelPlanItem.place), joinedload(TravelPlanItem.creator))
        .filter(TravelPlanItem.id == row.id)
        .one()
    )
    return _plan_item_to_out(row)


@app.post(
    "/api/travel-plans/{plan_id}/items",
    response_model=TravelPlanItemOut,
    status_code=status.HTTP_201_CREATED,
)
def create_travel_plan_item(
    plan_id: int,
    body: TravelPlanItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanItemOut:
    plan = _load_travel_plan(db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="일정표를 찾을 수 없습니다")
    return _create_plan_item_for_plan(db, plan, body, current_user)


# Compatibility for clients loaded during the rolling deployment. Both legacy
# routes now read/write the same city-shared plan rather than a private list.
@app.get("/api/plans/{city_id}", response_model=list[TravelPlanItemOut])
def list_plan_items(
    city_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TravelPlanItemOut]:
    plan = _get_city_shared_plan(db, city_id, current_user)
    output = _travel_plan_to_out(db, plan, current_user)
    return [item for day in output.days for item in day.items] + output.unscheduled_items


@app.post("/api/plans/{city_id}/items", response_model=TravelPlanItemOut, status_code=status.HTTP_201_CREATED)
def create_plan_item(
    city_id: int,
    body: TravelPlanItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanItemOut:
    plan = _get_city_shared_plan(db, city_id, current_user)
    return _create_plan_item_for_plan(db, plan, body, current_user)


@app.patch("/api/plan-items/{item_id}", response_model=TravelPlanItemOut)
def update_plan_item(
    item_id: int,
    body: TravelPlanItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelPlanItemOut:
    row = (
        db.query(TravelPlanItem)
        .options(joinedload(TravelPlanItem.place), joinedload(TravelPlanItem.creator))
        .filter(TravelPlanItem.id == item_id)
        .first()
    )
    if row is None or row.plan_id is None:
        raise HTTPException(status_code=404, detail="일정 항목을 찾을 수 없습니다")
    plan = _load_travel_plan(db, row.plan_id)
    if plan is None or not _can_edit_plan(plan, current_user.id):
        raise HTTPException(status_code=404, detail="편집할 일정 항목을 찾을 수 없습니다")
    values = body.model_dump(exclude_unset=True)
    if "plan_day_id" in values and values["plan_day_id"] is not None:
        day_exists = db.query(TravelPlanDay.id).filter(
            TravelPlanDay.id == values["plan_day_id"],
            TravelPlanDay.plan_id == plan.id,
        ).first()
        if day_exists is None:
            raise HTTPException(status_code=404, detail="이 일정표의 날짜를 찾을 수 없습니다")
    next_start = values.get("start_time", row.start_time)
    next_end = values.get("end_time", row.end_time)
    if next_end and not next_start:
        raise HTTPException(status_code=422, detail="종료 시간을 쓰려면 시작 시간도 필요합니다")
    if next_start and next_end and next_end <= next_start:
        raise HTTPException(status_code=422, detail="종료 시간은 시작 시간보다 뒤여야 합니다")
    for key, value in values.items():
        setattr(row, key, value.strip() if key == "note" and value is not None else value)
    plan.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    place = _load_place(db, row.place_id)
    assert place is not None
    row.place = place
    return _plan_item_to_out(row)


@app.delete("/api/plan-items/{item_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_plan_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    row = db.query(TravelPlanItem).filter(TravelPlanItem.id == item_id).first()
    if row is None or row.plan_id is None:
        raise HTTPException(status_code=404, detail="일정 항목을 찾을 수 없습니다")
    plan = _load_travel_plan(db, row.plan_id)
    if plan is None or not _can_edit_plan(plan, current_user.id):
        raise HTTPException(status_code=404, detail="편집할 일정 항목을 찾을 수 없습니다")
    plan.updated_at = datetime.now(timezone.utc)
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _chat_message_to_out(row: TravelChatMessage) -> TravelChatMessageOut:
    try:
        sources = json.loads(row.sources or "[]")
    except json.JSONDecodeError:
        sources = []
    try:
        place_ids = json.loads(row.place_ids or "[]")
    except json.JSONDecodeError:
        place_ids = []
    try:
        candidates = json.loads(row.candidates or "[]")
    except (AttributeError, json.JSONDecodeError):
        candidates = []
    return TravelChatMessageOut(
        id=row.id,
        city_id=row.city_id,
        role=row.role,
        content=row.content,
        sources=sources if isinstance(sources, list) else [],
        place_ids=place_ids if isinstance(place_ids, list) else [],
        candidates=candidates if isinstance(candidates, list) else [],
        created_at=row.created_at,
    )


@app.get("/api/travel-chat", response_model=list[TravelChatMessageOut])
def list_travel_chat(
    city_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TravelChatMessageOut]:
    rows = (
        db.query(TravelChatMessage)
        .filter(TravelChatMessage.user_id == current_user.id, TravelChatMessage.city_id == city_id)
        .order_by(TravelChatMessage.id.desc())
        .limit(80)
        .all()
    )
    rows.reverse()
    return [_chat_message_to_out(row) for row in rows]


@app.get("/api/travel-profile", response_model=TravelProfileOut)
def get_travel_profile(
    city_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelProfileOut:
    from app.personalization import build_user_travel_profile

    city_exists = db.query(City.id).filter(City.id == city_id, City.status == "active").first()
    if city_exists is None:
        raise HTTPException(status_code=404, detail="활성 도시를 찾을 수 없습니다")
    return TravelProfileOut(**build_user_travel_profile(db, user_id=current_user.id, city_id=city_id))


@app.delete("/api/travel-chat", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def clear_travel_chat(
    city_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    db.query(TravelChatMessage).filter(
        TravelChatMessage.user_id == current_user.id,
        TravelChatMessage.city_id == city_id,
    ).delete(synchronize_session=False)
    db.query(TravelChatWork).filter(
        TravelChatWork.user_id == current_user.id,
        TravelChatWork.city_id == city_id,
    ).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/travel-chat", response_model=TravelChatResponse)
def post_travel_chat(
    body: TravelChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TravelChatResponse:
    from app.travel_chat import answer_travel_chat

    try:
        result = answer_travel_chat(
            db,
            user_id=current_user.id,
            city_id=body.city_id,
            message=body.message.strip(),
            selected_place_id=body.selected_place_id,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TravelChatResponse(
        message=_chat_message_to_out(result["row"]),
        model=result["model"],
        grounded_place_ids=result["place_ids"],
    )


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
