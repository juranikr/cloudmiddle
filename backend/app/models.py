import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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


class PlaceEventAction(str, enum.Enum):
    create = "create"
    update = "update"
    delete = "delete"
    merge = "merge"
    image_add = "image_add"
    image_reorder = "image_reorder"
    context_update = "context_update"
    agent_create = "agent_create"
    appeal = "appeal"
    rollback = "rollback"


class UserMessageKind(str, enum.Enum):
    agent_merge = "agent_merge"
    agent_create = "agent_create"
    appeal_result = "appeal_result"
    system = "system"


class PlaceAppealStatus(str, enum.Enum):
    open = "open"
    resolved = "resolved"
    dismissed = "dismissed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    markers: Mapped[list["Marker"]] = relationship(back_populates="creator")
    contributions: Mapped[list["PlaceContributor"]] = relationship(back_populates="user")
    messages: Mapped[list["UserMessage"]] = relationship(back_populates="user")
    appeals: Mapped[list["PlaceAppeal"]] = relationship(back_populates="user")
    favorites: Mapped[list["PlaceFavorite"]] = relationship(back_populates="user")


class Marker(Base):
    """공유 장소(핀/구역). 단일 소유자 모델이 아니라 기여자 집합으로 관리."""

    __tablename__ = "markers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 최초 등록자(참고용). 수정 권한은 로그인 사용자 전원.
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    category: Mapped[MarkerCategory] = mapped_column(
        Enum(
            MarkerCategory,
            name="marker_category",
            native_enum=False,
            values_callable=lambda e: [x.value for x in e],
        ),
        nullable=False,
    )
    shape: Mapped[MarkerShape] = mapped_column(
        Enum(
            MarkerShape,
            name="marker_shape",
            native_enum=False,
            values_callable=lambda e: [x.value for x in e],
        ),
        default=MarkerShape.point,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    polygon: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    agent_context: Mapped[str] = mapped_column(Text, default="", nullable=False)
    merged_into_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_agent_suggested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 에이전트가 마지막으로 유효성(폐업·이전 여부)을 재검증한 시각
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    creator: Mapped[Optional[User]] = relationship(back_populates="markers")
    contributors: Mapped[list["PlaceContributor"]] = relationship(
        back_populates="place", cascade="all, delete-orphan"
    )
    images: Mapped[list["PlaceImage"]] = relationship(
        back_populates="place", cascade="all, delete-orphan", order_by="PlaceImage.sort_order"
    )
    # 삭제 후에도 place_events는 이력으로 남김 (FK ON DELETE SET NULL)
    events: Mapped[list["PlaceEvent"]] = relationship(back_populates="place")


class PlaceContributor(Base):
    __tablename__ = "place_contributors"
    __table_args__ = (UniqueConstraint("place_id", "user_id", name="uq_place_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("markers.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    place: Mapped[Marker] = relationship(back_populates="contributors")
    user: Mapped[User] = relationship(back_populates="contributions")


class PlaceEvent(Base):
    """핀/구역 추가·수정·병합 등 모든 이력. Groq 에이전트가 미읽음만 처리."""

    __tablename__ = "place_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    actor: Mapped[str] = mapped_column(String(20), default="user", nullable=False)  # user|agent|system
    action: Mapped[PlaceEventAction] = mapped_column(
        Enum(
            PlaceEventAction,
            name="place_event_action",
            native_enum=False,
            values_callable=lambda e: [x.value for x in e],
        ),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    groq_read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    place: Mapped[Optional[Marker]] = relationship(back_populates="events")


class PlaceImage(Base):
    __tablename__ = "place_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("markers.id", ondelete="CASCADE"), index=True)
    s3_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), default="image/jpeg", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    group_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    uploaded_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    place: Mapped[Marker] = relationship(back_populates="images")


class UserMessage(Base):
    """인앱 알림. 에이전트 병합/추가·이의 처리 결과 등."""

    __tablename__ = "user_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[UserMessageKind] = mapped_column(
        Enum(
            UserMessageKind,
            name="user_message_kind",
            native_enum=False,
            values_callable=lambda e: [x.value for x in e],
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    related_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("place_events.id", ondelete="SET NULL"), nullable=True
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    user: Mapped[User] = relationship(back_populates="messages")


class PlaceAppeal(Base):
    """에이전트 조치에 대한 이의. 다음 주기(미읽음)에 재고려."""

    __tablename__ = "place_appeals"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("markers.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_messages.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PlaceAppealStatus] = mapped_column(
        Enum(
            PlaceAppealStatus,
            name="place_appeal_status",
            native_enum=False,
            values_callable=lambda e: [x.value for x in e],
        ),
        default=PlaceAppealStatus.open,
        nullable=False,
    )
    agent_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    groq_read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="appeals")

class AgentKnowledge(Base):
    """에이전트 장기 교훈/지식. 이의·롤백·웹조사 결과를 주제별로 병합 저장."""

    __tablename__ = "agent_knowledge"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentSearchLog(Base):
    """에이전트 웹 검색 이력. 어떤 키워드를 언제 조사했고 새 콘텐츠가 얼마나 나왔는지."""

    __tablename__ = "agent_search_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    results_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AgentWebVisit(Base):
    """에이전트가 열람한 웹 페이지. 같은 콘텐츠 재열람 방지용."""

    __tablename__ = "agent_web_visits"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    visit_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_visited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_visited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlaceFavorite(Base):
    __tablename__ = "place_favorites"
    __table_args__ = (UniqueConstraint("user_id", "place_id", name="uq_user_favorite_place"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("markers.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="favorites")
    place: Mapped[Marker] = relationship()

