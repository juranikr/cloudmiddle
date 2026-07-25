from datetime import datetime
from typing import Optional

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


class MarkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    author_name: str
    category: MarkerCategory
    shape: MarkerShape
    title: str
    description: str
    lat: float
    lng: float
    polygon: Optional[list[LatLng]] = None
    created_at: datetime
    updated_at: datetime
