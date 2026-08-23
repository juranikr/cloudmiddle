import enum
from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Date,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
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
    notes: Mapped[list["PlaceNote"]] = relationship(back_populates="user")


class City(Base):
    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    name_ko: Mapped[str] = mapped_column(String(100), nullable=False)
    name_local: Mapped[str] = mapped_column(String(100), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), default="CN", nullable=False)
    center_lat: Mapped[float] = mapped_column(Float, nullable=False)
    center_lng: Mapped[float] = mapped_column(Float, nullable=False)
    default_zoom: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    search_viewbox: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    search_context: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    markers: Mapped[list["Marker"]] = relationship(back_populates="city")


class Marker(Base):
    """공유 장소(핀/구역). 단일 소유자 모델이 아니라 기여자 집합으로 관리."""

    __tablename__ = "markers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 최초 등록자(참고용). 수정 권한은 로그인 사용자 전원.
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    city_id: Mapped[int] = mapped_column(
        ForeignKey("cities.id", ondelete="RESTRICT"), index=True, nullable=False, default=1
    )
    zone_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    chain_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("place_chains.id", ondelete="SET NULL"), index=True, nullable=True
    )
    branch_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    # A place's role in a balanced trip. Kept separate from the map icon category so
    # research can measure whether a city has more than just attractions/museums.
    travel_role: Mapped[str] = mapped_column(String(30), default="general", index=True, nullable=False)
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
    # 좌표가 어디에서 왔고 얼마나 믿을 만한지 보존한다. 중국 지도 좌표계 혼동도 함께 방지.
    coordinate_source: Mapped[str] = mapped_column(String(50), default="manual", nullable=False)
    coordinate_external_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    coordinate_query: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    coordinate_source_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    coordinate_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    coordinate_crs: Mapped[str] = mapped_column(String(20), default="WGS84", nullable=False)
    coordinate_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    city: Mapped[City] = relationship(back_populates="markers")
    contributors: Mapped[list["PlaceContributor"]] = relationship(
        back_populates="place", cascade="all, delete-orphan"
    )
    images: Mapped[list["PlaceImage"]] = relationship(
        back_populates="place", cascade="all, delete-orphan", order_by="PlaceImage.sort_order"
    )
    insights: Mapped[list["PlaceInsight"]] = relationship(
        back_populates="place", cascade="all, delete-orphan", order_by="PlaceInsight.sort_order"
    )
    notes: Mapped[list["PlaceNote"]] = relationship(
        back_populates="place", cascade="all, delete-orphan", order_by="PlaceNote.created_at"
    )
    chain: Mapped[Optional["PlaceChain"]] = relationship(back_populates="branches")
    zone: Mapped[Optional["Marker"]] = relationship(
        remote_side="Marker.id", foreign_keys=[zone_id], back_populates="zone_members"
    )
    zone_members: Mapped[list["Marker"]] = relationship(
        foreign_keys=[zone_id], back_populates="zone"
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


class PlaceInsight(Base):
    """위치·역사·방문정보를 출처/신뢰도와 함께 저장하는 작은 지식 단위."""

    __tablename__ = "place_insights"
    __table_args__ = (
        UniqueConstraint("place_id", "kind", "title", name="uq_place_insight_title"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("markers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True, nullable=False)  # location|history|visit|tip
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    year_label: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    source_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    source_title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    created_by: Mapped[str] = mapped_column(String(20), default="agent", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    place: Mapped[Marker] = relationship(back_populates="insights")


class PlaceNote(Base):
    """장소 본문과 분리된 사용자별 메모/댓글."""

    __tablename__ = "place_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("markers.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(String(20), default="shared", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    place: Mapped[Marker] = relationship(back_populates="notes")
    user: Mapped[User] = relationship(back_populates="notes")


class PlaceChain(Base):
    """브랜드/체인 본체. Marker는 실제 지점이며 chain_id로 묶인다."""

    __tablename__ = "place_chains"
    __table_args__ = (UniqueConstraint("name_local", "name_ko", name="uq_place_chain_names"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name_local: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    name_ko: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(30), default="other", nullable=False)
    aliases: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    branches: Mapped[list[Marker]] = relationship(back_populates="chain")


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
    scope: Mapped[str] = mapped_column(String(20), default="global", nullable=False, index=True)
    city_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), index=True, nullable=True
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    category: Mapped[str] = mapped_column(String(30), default="playbook", nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    principles: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    next_actions: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    keywords: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    applicability: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    source_refs: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    retrieval_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_retrieved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentSearchLog(Base):
    """에이전트 웹 검색 이력. 어떤 키워드를 언제 조사했고 새 콘텐츠가 얼마나 나왔는지."""

    __tablename__ = "agent_search_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    city_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), index=True, nullable=True
    )
    results_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AgentSearchResult(Base):
    """URLs observed in search results, including pages the agent did not open."""

    __tablename__ = "agent_search_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    city_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), index=True, nullable=True
    )
    seen_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentWebVisit(Base):
    """에이전트가 열람한 웹 페이지. 같은 콘텐츠 재열람 방지용."""

    __tablename__ = "agent_web_visits"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    city_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), index=True, nullable=True
    )
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


class AgentProposal(Base):
    """에이전트의 고위험 변경은 즉시 반영하지 않고 근거가 있는 제안으로 보관한다."""

    __tablename__ = "agent_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    result_place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    evidence: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_urls: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    proposal_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    decision_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    decided_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentKnowledgeArchive(Base):
    """정리 전 장문 지식 원본을 보존하는 별도 시스템 기록."""

    __tablename__ = "agent_knowledge_archive"

    id: Mapped[int] = mapped_column(primary_key=True)
    original_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    topic: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="global", nullable=False)
    city_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    place_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    archived_reason: Mapped[str] = mapped_column(String(200), default="knowledge_rebuild", nullable=False)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AgentRun(Base):
    """토큰이 아닌 성과를 기록하는 에이전트 실행 단위."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    mission_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_missions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    work_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_work_items.id", ondelete="SET NULL"), index=True, nullable=True
    )
    mode: Mapped[str] = mapped_column(String(30), default="queue", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True, nullable=False)
    objective: Mapped[str] = mapped_column(Text, default="", nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    metrics: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list["AgentRunStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentRunStep.sequence"
    )


class AgentRunStep(Base):
    __tablename__ = "agent_run_steps"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_agent_run_step_sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(30), default="act", nullable=False)
    tool: Mapped[str] = mapped_column(String(80), default="", index=True, nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)
    score_delta: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[AgentRun] = relationship(back_populates="steps")


class AgentTask(Base):
    """다음 사이클이 이어받는 조사/정제 백로그."""

    __tablename__ = "agent_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="research", index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    success_metric: Mapped[str] = mapped_column(Text, default="", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentQualityGapDisposition(Base):
    """Auditable terminal/cooldown state for one exact place quality gap.

    A managed quality task is derived from live marker fields, so a gap that is
    valid but currently impossible (for example no exact freely licensed image)
    must be represented separately from the task row.  The condition fingerprint
    lets the scheduler retry only when the place, zone catalogue, source set, or
    an explicit cooldown condition has actually changed.
    """

    __tablename__ = "agent_quality_gap_dispositions"
    __table_args__ = (
        UniqueConstraint("place_id", "gap_kind", name="uq_agent_quality_gap_place_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    place_id: Mapped[int] = mapped_column(
        ForeignKey("markers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    gap_kind: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    # blocked | source_exhausted | waived are active dispositions.  resolved
    # and reopened retain the audit row without suppressing future work.
    status: Mapped[str] = mapped_column(String(30), default="blocked", index=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_refs: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    condition_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    retry_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class AgentMission(Base):
    """A durable objective that survives individual model calls and batch runs."""

    __tablename__ = "agent_missions"

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40), default="research", index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    objective: Mapped[str] = mapped_column(Text, default="", nullable=False)
    success_metric: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    progress: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentWorkItem(Base):
    """Small resumable target within a mission, normally one place or one exact gap."""

    __tablename__ = "agent_work_items"
    __table_args__ = (UniqueConstraint("mission_id", "target_key", name="uq_agent_mission_target"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mission_id: Mapped[int] = mapped_column(
        ForeignKey("agent_missions.id", ondelete="CASCADE"), index=True
    )
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    target_type: Mapped[str] = mapped_column(String(30), default="task", nullable=False)
    target_key: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    goal: Mapped[str] = mapped_column(Text, default="", nullable=False)
    definition_of_done: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stage: Mapped[str] = mapped_column(String(30), default="observe", index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ready", index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True, nullable=False)
    state_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    current_hypothesis: Mapped[str] = mapped_column(Text, default="", nullable=False)
    next_action: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    failed_approaches: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    blocked_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    retry_condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentCheckpoint(Base):
    """Compact task state persisted after each action; never private chain-of-thought."""

    __tablename__ = "agent_checkpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    mission_id: Mapped[int] = mapped_column(
        ForeignKey("agent_missions.id", ondelete="CASCADE"), index=True
    )
    work_item_id: Mapped[int] = mapped_column(
        ForeignKey("agent_work_items.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    decision: Mapped[str] = mapped_column(Text, default="", nullable=False)
    new_facts: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    rejected_claims: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    failed_approaches: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    next_action: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), default="observed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AgentEvidence(Base):
    """A claim-level research observation, including rejected and blocked sources."""

    __tablename__ = "agent_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    mission_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_missions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    work_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_work_items.id", ondelete="SET NULL"), index=True, nullable=True
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(30), default="tool", index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(1000), default="", index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    claim: Mapped[str] = mapped_column(Text, default="", nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_status: Mapped[str] = mapped_column(String(30), default="discovered", index=True, nullable=False)
    rejection_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AgentLesson(Base):
    """An operational lesson promoted only after repeated outcome evidence."""

    __tablename__ = "agent_lessons"
    __table_args__ = (UniqueConstraint("lesson_key", name="uq_agent_lesson_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_key: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="global", index=True, nullable=False)
    city_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cities.id", ondelete="CASCADE"), index=True, nullable=True
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("markers.id", ondelete="SET NULL"), index=True, nullable=True
    )
    category: Mapped[str] = mapped_column(String(40), default="workflow", index=True, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    expected_effect: Mapped[str] = mapped_column(Text, default="", nullable=False)
    applicability: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    evidence_refs: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="candidate", index=True, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applied_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_applied_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class AgentKnowledgeUse(Base):
    """Audit trail of why a knowledge item or lesson entered a run context."""

    __tablename__ = "agent_knowledge_uses"

    id: Mapped[int] = mapped_column(primary_key=True)
    knowledge_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_knowledge.id", ondelete="SET NULL"), index=True, nullable=True
    )
    lesson_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_lessons.id", ondelete="SET NULL"), index=True, nullable=True
    )
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    mission_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_missions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    work_item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_work_items.id", ondelete="SET NULL"), index=True, nullable=True
    )
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    retrieval_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class TravelPlan(Base):
    """A shareable itinerary document, ready for private/public/member modes."""

    __tablename__ = "travel_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # city_shared: every signed-in user can edit the city's common board.
    # private/shared/public are reserved for personal drafts, invited members,
    # and published itinerary posts respectively.
    visibility: Mapped[str] = mapped_column(String(20), default="city_shared", index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="published", index=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Shanghai", nullable=False)
    cover_image_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[Optional[User]] = relationship(foreign_keys=[owner_user_id])
    members: Mapped[list["TravelPlanMember"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )
    days: Mapped[list["TravelPlanDay"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="TravelPlanDay.calendar_date"
    )
    items: Mapped[list["TravelPlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class TravelPlanMember(Base):
    """Explicit itinerary membership for invited sharing and future posts."""

    __tablename__ = "travel_plan_members"
    __table_args__ = (UniqueConstraint("plan_id", "user_id", name="uq_travel_plan_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("travel_plans.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer", nullable=False)
    invitation_status: Mapped[str] = mapped_column(String(20), default="accepted", index=True, nullable=False)
    invited_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    plan: Mapped[TravelPlan] = relationship(back_populates="members")
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    invited_by: Mapped[Optional[User]] = relationship(foreign_keys=[invited_by_user_id])


class TravelPlanDay(Base):
    """A freely chosen calendar date inside an itinerary."""

    __tablename__ = "travel_plan_days"
    __table_args__ = (UniqueConstraint("plan_id", "calendar_date", name="uq_travel_plan_calendar_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("travel_plans.id", ondelete="CASCADE"), index=True)
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    plan: Mapped[TravelPlan] = relationship(back_populates="days")
    creator: Mapped[Optional[User]] = relationship(foreign_keys=[created_by_user_id])
    items: Mapped[list["TravelPlanItem"]] = relationship(back_populates="plan_day")


class TravelPlanItem(Base):
    """A place scheduled at an optional exact time on a shareable plan."""

    __tablename__ = "travel_plan_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("travel_plans.id", ondelete="CASCADE"), index=True, nullable=True
    )
    plan_day_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("travel_plan_days.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # Retained as the creator identity so existing personalization history is
    # preserved while the plan itself becomes shared.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("markers.id", ondelete="CASCADE"), index=True)
    day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    slot: Mapped[str] = mapped_column(String(20), default="afternoon", nullable=False)
    start_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    end_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    plan: Mapped[Optional[TravelPlan]] = relationship(back_populates="items")
    plan_day: Mapped[Optional[TravelPlanDay]] = relationship(back_populates="items")
    creator: Mapped[User] = relationship(foreign_keys=[user_id])
    place: Mapped[Marker] = relationship()


class TravelChatMessage(Base):
    """Grounded travel-agent conversation history, separated by user and city."""

    __tablename__ = "travel_chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    place_ids: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # Research candidates are kept separately from assistant prose.  A short
    # follow-up such as "등록해줘" can therefore refer to the exact grounded
    # business without trusting or reparsing an earlier natural-language claim.
    candidates: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # Compact, inspectable system history for this turn.  It intentionally keeps
    # tool names, arguments, outcomes and evidence URLs rather than model tokens.
    tool_trace: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class TravelChatWork(Base):
    """Durable, resumable work behind a user's multi-turn travel request."""

    __tablename__ = "travel_chat_work"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), default="answer", nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="unspecified", nullable=False)
    subject: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    goal: Mapped[str] = mapped_column(Text, default="", nullable=False)
    requested_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Candidate/action state is intentionally separate from prose.  A later
    # "continue" or "add the rest" resumes these exact entities rather than
    # asking the model to reconstruct a task from its previous answer.
    state: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )

