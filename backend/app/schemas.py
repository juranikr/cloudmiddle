from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models import MarkerCategory, MarkerShape


class LatLng(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class CityOut(BaseModel):
    id: int
    slug: str
    name_ko: str
    name_local: str
    country_code: str
    center_lat: float
    center_lng: float
    default_zoom: int
    search_viewbox: str = ""
    status: str
    place_count: int = 0
    zone_count: int = 0


class GeocodeResult(BaseModel):
    query: str = ""
    display_name: str
    lat: float
    lng: float
    type: str = ""
    source: str = ""
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    confidence_label: str = "확인 필요"
    storage_allowed: bool = True
    existing_marker_id: Optional[int] = None
    external_id: str = ""
    source_url: str = ""


class ShareImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="", max_length=20, description="amap | dianping | 자동")
    city_id: int = Field(default=1, gt=0)


class ShareImportResultOut(BaseModel):
    source: str
    title: str
    description: str
    address: str = ""
    source_url: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    category_hint: str = "other"
    needs_map_pick: bool = False
    note: str = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    display_name: str
    created_at: datetime
    is_admin: bool = False


class MarkerCreate(BaseModel):
    city_id: int = Field(default=1, gt=0)
    category: MarkerCategory
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    shape: MarkerShape = MarkerShape.point
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    polygon: Optional[list[LatLng]] = None
    coordinate_source: str = Field(default="manual", max_length=50)
    coordinate_external_id: str = Field(default="", max_length=200)
    coordinate_query: str = Field(default="", max_length=300)
    coordinate_source_url: str = Field(default="", max_length=1000)
    coordinate_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    coordinate_crs: str = Field(default="WGS84", max_length=20)
    zone_id: Optional[int] = Field(default=None, gt=0)
    chain_id: Optional[int] = Field(default=None, gt=0)
    branch_name: str = Field(default="", max_length=120)
    travel_role: str = Field(
        default="general",
        pattern="^(history|food|market_night|neighborhood|nature|shopping|rest|practical|general)$",
    )

    @model_validator(mode="after")
    def validate_geometry(self) -> "MarkerCreate":
        if self.shape == MarkerShape.polygon:
            if not self.polygon or len(self.polygon) < 3:
                raise ValueError("구역은 꼭짓점이 3개 이상 필요합니다")
        return self


class MarkerUpdate(BaseModel):
    category: Optional[MarkerCategory] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)
    polygon: Optional[list[LatLng]] = None
    zone_id: Optional[int] = Field(default=None, gt=0)
    chain_id: Optional[int] = Field(default=None, gt=0)
    branch_name: Optional[str] = Field(default=None, max_length=120)
    travel_role: Optional[str] = Field(
        default=None,
        pattern="^(history|food|market_night|neighborhood|nature|shopping|rest|practical|general)$",
    )


class PlaceImageOut(BaseModel):
    id: int
    url: str
    sort_order: int
    group_key: Optional[str] = None
    content_type: str


class PlaceInsightOut(BaseModel):
    id: int
    kind: str
    title: str
    content: str
    year_label: str = ""
    source_url: str = ""
    source_title: str = ""
    confidence: float = 0.0
    verified_at: Optional[datetime] = None


class PlaceNoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    visibility: str = Field(default="shared", pattern="^(shared|private)$")


class PlaceNoteUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    visibility: Optional[str] = Field(default=None, pattern="^(shared|private)$")


class PlaceNoteOut(BaseModel):
    id: int
    place_id: int
    user_id: int
    author_name: str
    body: str
    visibility: str
    is_mine: bool = False
    created_at: datetime
    updated_at: datetime


class PlaceChainCreate(BaseModel):
    name_local: str = Field(min_length=1, max_length=160)
    name_ko: str = Field(default="", max_length=160)
    category: str = Field(default="other", max_length=30)
    aliases: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)


class PlaceChainOut(BaseModel):
    id: int
    name_local: str
    name_ko: str
    category: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    branch_count: int = 0
    created_at: datetime
    updated_at: datetime


class MarkerOut(BaseModel):
    id: int
    city_id: int
    user_id: Optional[int] = None
    author_name: str = ""
    contributor_names: list[str] = []
    category: MarkerCategory
    shape: MarkerShape
    title: str
    description: str
    agent_context: str = ""
    lat: float
    lng: float
    polygon: Optional[list[LatLng]] = None
    images: list[PlaceImageOut] = []
    insights: list[PlaceInsightOut] = []
    zone_id: Optional[int] = None
    zone_title: str = ""
    chain_id: Optional[int] = None
    chain_name: str = ""
    branch_name: str = ""
    travel_role: str = "general"
    note_count: int = 0
    coordinate_source: str = "manual"
    coordinate_external_id: str = ""
    coordinate_query: str = ""
    coordinate_source_url: str = ""
    coordinate_confidence: Optional[float] = None
    coordinate_crs: str = "WGS84"
    coordinate_verified_at: Optional[datetime] = None
    is_agent_suggested: bool = False
    is_favorite: bool = False
    created_at: datetime
    updated_at: datetime


class ImageUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_type: str = Field(default="image/jpeg", max_length=100)


class ImageUploadResponse(BaseModel):
    image_id: int
    upload_url: str
    public_url: str
    s3_key: str


class ImageReorderRequest(BaseModel):
    image_ids: list[int]


class AgentRunResponse(BaseModel):
    ok: bool
    status: str = "completed"
    steps: int
    message: str
    unread_before: int
    unread_after: int
    city_id: int = 1
    score: float = 0.0
    performance: dict[str, int] = Field(default_factory=dict)
    remaining_gaps: list[str] = Field(default_factory=list)
    run_id: Optional[int] = None


class UserMessageOut(BaseModel):
    id: int
    place_id: Optional[int] = None
    kind: str
    title: str
    body: str
    read_at: Optional[datetime] = None
    created_at: datetime
    can_appeal: bool = False


class AppealCreate(BaseModel):
    place_id: int
    body: str = Field(min_length=2, max_length=4000)
    message_id: Optional[int] = None


class AppealOut(BaseModel):
    id: int
    place_id: int
    body: str
    status: str
    agent_note: str = ""
    created_at: datetime
    resolved_at: Optional[datetime] = None


class PlaceEventChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class PlaceEventOut(BaseModel):
    id: int
    place_id: Optional[int] = None
    user_id: Optional[int] = None
    actor_name: str = ""
    actor: str
    action: str
    summary: str
    changes: list[PlaceEventChange] = []
    groq_read: bool = False
    created_at: datetime


class AgentKnowledgeOut(BaseModel):
    id: int
    topic: str
    title: str
    content: str
    scope: str = "global"
    city_id: Optional[int] = None
    place_id: Optional[int] = None
    category: str = "playbook"
    summary: str = ""
    principles: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    evidence_count: int = 0
    quality_score: float = 0.0
    status: str = "active"
    version: int = 1
    created_at: datetime
    updated_at: datetime


class FavoriteToggleOut(BaseModel):
    place_id: int
    is_favorite: bool


class TravelPlanItemCreate(BaseModel):
    place_id: int = Field(gt=0)
    day: int = Field(default=1, ge=1, le=14)
    slot: str = Field(default="afternoon", pattern="^(morning|afternoon|evening)$")
    note: str = Field(default="", max_length=1000)


class TravelPlanItemUpdate(BaseModel):
    day: Optional[int] = Field(default=None, ge=1, le=14)
    slot: Optional[str] = Field(default=None, pattern="^(morning|afternoon|evening)$")
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)
    note: Optional[str] = Field(default=None, max_length=1000)


class TravelPlanItemOut(BaseModel):
    id: int
    city_id: int
    place_id: int
    day: int
    slot: str
    sort_order: int
    note: str = ""
    place: MarkerOut
    created_at: datetime
    updated_at: datetime


class TravelChatRequest(BaseModel):
    city_id: int = Field(gt=0)
    message: str = Field(min_length=1, max_length=3000)
    selected_place_id: Optional[int] = Field(default=None, gt=0)


class TravelChatMessageOut(BaseModel):
    id: int
    city_id: int
    role: str
    content: str
    sources: list[str] = Field(default_factory=list)
    place_ids: list[int] = Field(default_factory=list)
    created_at: datetime


class TravelChatResponse(BaseModel):
    message: TravelChatMessageOut
    model: str
    grounded_place_ids: list[int] = Field(default_factory=list)


class TravelPreferenceSignalOut(BaseModel):
    key: str
    label: str
    score: float = 0.0
    evidence_count: int = 0


class TravelAnchorOut(BaseModel):
    place_id: int
    title: str
    lat: float
    lng: float
    zone: str = ""
    sources: list[str] = Field(default_factory=list)


class PersonalizedPlaceRecommendationOut(BaseModel):
    place_id: int
    title: str
    category: str
    travel_role: str = "general"
    zone: str = ""
    score: float
    reason: str
    distance_km: Optional[float] = None


class TravelProfileOut(BaseModel):
    user_id: int
    city_id: int
    signals: list[TravelPreferenceSignalOut] = Field(default_factory=list)
    anchors: list[TravelAnchorOut] = Field(default_factory=list)
    recommendations: list[PersonalizedPlaceRecommendationOut] = Field(default_factory=list)
    category_scores: dict[str, float] = Field(default_factory=dict)
    role_scores: dict[str, float] = Field(default_factory=dict)
    brand_scores: dict[str, float] = Field(default_factory=dict)
    favorite_place_ids: list[int] = Field(default_factory=list)
    created_place_ids: list[int] = Field(default_factory=list)
    direct_source_counts: dict[str, int] = Field(default_factory=dict)
    corrections: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, int] = Field(default_factory=dict)
