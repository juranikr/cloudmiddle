from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models import MarkerCategory, MarkerShape


class LatLng(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class GeocodeResult(BaseModel):
    display_name: str
    lat: float
    lng: float
    type: str = ""


class ShareImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="", max_length=20, description="amap | dianping | 자동")


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
    category: MarkerCategory
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    shape: MarkerShape = MarkerShape.point
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    polygon: Optional[list[LatLng]] = None

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


class PlaceImageOut(BaseModel):
    id: int
    url: str
    sort_order: int
    group_key: Optional[str] = None
    content_type: str


class MarkerOut(BaseModel):
    id: int
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
    steps: int
    message: str
    unread_before: int
    unread_after: int


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
    place_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class FavoriteToggleOut(BaseModel):
    place_id: int
    is_favorite: bool
