import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class MarkerCategory(str, enum.Enum):
    tourist = "tourist"
    lodging = "lodging"
    restaurant = "restaurant"
    transport = "transport"
    shopping = "shopping"
    drink = "drink"
    convenience = "convenience"
    other = "other"


class MarkerShape(str, enum.Enum):
    point = "point"
    polygon = "polygon"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    markers: Mapped[list["Marker"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Marker(Base):
    __tablename__ = "markers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category: Mapped[MarkerCategory] = mapped_column(
        Enum(MarkerCategory, name="marker_category", native_enum=False, values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )
    shape: Mapped[MarkerShape] = mapped_column(
        Enum(MarkerShape, name="marker_shape", native_enum=False, values_callable=lambda e: [x.value for x in e]),
        default=MarkerShape.point,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    # JSON: [{"lat": number, "lng": number}, ...] — polygon일 때만 사용
    polygon: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="markers")
